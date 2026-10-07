import logging
import os
import re
import difflib
import hashlib
from concurrent.futures import ThreadPoolExecutor
from string import Template
from typing import Callable, List, Optional, Union
import json
from concurrent.futures import as_completed

from openai import OpenAI

from app.config import CACHE_PATH
from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.storage.cache_manager import CacheManager
from app.core.subtitle_processor.prompt import (
    PROMPT_SPLIT_SEMANTIC,
    PROMPT_SPLIT_SENTENCE,
    PROMPT_SPLIT_SENTENCE_RESTORE,
    get_prompt_template,
)
from app.core.utils.logger import setup_logger
from app.core.utils.openai_compat import (
    extract_openai_usage,
    format_openai_compat_error,
    get_openai_compat_request_options,
)

logger = setup_logger("subtitle_splitter")

# 字幕分段的配置常量
MAX_WORD_COUNT_CJK = 25  # 中日韩文本最大字数


MAX_WORD_COUNT_ENGLISH = 18  # 英文文本最大单词数
SEGMENT_THRESHOLD = 300  # 每个分段的最大字数
MAX_GAP = 1500  # 允许每个词语之间的最大时间间隔（毫秒）
SHORT_DISPLAY_GAP_FILL_MS = 500  # 断句后自动补齐不超过 0.5 秒的显示空隙
SPLIT_STRATEGY_VERSION = "lossless-sequential-alignment-v3-abbreviation-aware"


def is_pure_punctuation(text: str) -> bool:
    """
    检查字符串是否仅由标点符号组成

    Args:
        text: 待检查的文本

    Returns:
        bool: 是否仅包含标点符号
    """
    return not re.search(r"\w", text, flags=re.UNICODE)


def is_mainly_cjk(text: str) -> bool:
    """
    判断文本是否主要由中日韩文字组成

    Args:
        text: 输入文本

    Returns:
        bool: 如果CJK字符占比超过50%则返回True
    """
    # 定义以单字形式出现的语言字符范围
    cjk_patterns = [
        # 东亚语言
        r"[\u4e00-\u9fff]",  # 中日韩统一表意文字
        r"[\u3040-\u309f]",  # 日文平假名
        r"[\u30a0-\u30ff]",  # 日文片假名
        r"[\uac00-\ud7af]",  # 韩文音节
        r"[\u3130-\u318f]",  # 韩文兼容字母
        # 东南亚语言
        r"[\u0e00-\u0e7f]",  # 泰文
        r"[\u0e80-\u0eff]",  # 老挝文
        r"[\u1000-\u109f]",  # 缅甸文
        r"[\u1780-\u17ff]",  # 高棉文(柬埔寨)
        # 南亚语言
        r"[\u0900-\u097f]",  # 天城文(印地语等)
        r"[\u0980-\u09ff]",  # 孟加拉语
        r"[\u0a00-\u0a7f]",  # 古木基文(旁遮普语)
        r"[\u0b00-\u0b7f]",  # 奥里亚文
        r"[\u0c00-\u0c7f]",  # 泰卢固文
        r"[\u0c80-\u0cff]",  # 卡纳达文
        r"[\u0d00-\u0d7f]",  # 马拉雅拉姆文
        r"[\u0d80-\u0dff]",  # 僧伽罗文(斯里兰卡)
        # 其他
        r"[\u1100-\u11ff]",  # 谚文字母
        r"[\u3100-\u312f]",  # 注音符号
    ]

    # 计算CJK字符数
    cjk_count = 0
    for pattern in cjk_patterns:
        cjk_count += len(re.findall(pattern, text))

    # 计算总字符数（不包括空白字符）
    total_chars = len("".join(text.split()))

    # 如果CJK字符占比超过50%，则认为主要是CJK文本
    return cjk_count / total_chars > 0.5 if total_chars > 0 else False


def count_words(text: str) -> int:
    """
    统计多语言文本中的字符/单词数
    支持:
    - 英文（按空格分词）
    - CJK文字（中日韩统一表意文字）
    - 韩文/谚文
    - 泰文
    - 阿拉伯文
    - 俄文西里尔字母
    - 希伯来文
    - 越南文
    每个字符都计为1个单位，英文按照空格分词计数

    Args:
        text: 输入文本

    Returns:
        int: 字符/单词总数
    """
    # 定义各种语言的Unicode范围
    patterns = [
        r"[\u4e00-\u9fff]",  # 中日韩统一表意文字
        r"[\u3040-\u309f]",  # 平假名
        r"[\u30a0-\u30ff]",  # 片假名
        r"[\uac00-\ud7af]",  # 韩文音节
        r"[\u0e00-\u0e7f]",  # 泰文
        r"[\u0600-\u06ff]",  # 阿拉伯文
        r"[\u0400-\u04ff]",  # 西里尔字母（俄文等）
        r"[\u0590-\u05ff]",  # 希伯来文
        r"[\u1e00-\u1eff]",  # 越南文
        r"[\u3130-\u318f]",  # 韩文兼容字母
    ]

    # 统计所有非英文字符
    non_english_chars = 0
    remaining_text = text

    for pattern in patterns:
        # 计算当前语言的字符数
        chars = len(re.findall(pattern, remaining_text))
        non_english_chars += chars
        # 从文本中移除已计数的字符
        remaining_text = re.sub(pattern, " ", remaining_text)

    # 计算英文单词数（处理剩余的文本）
    english_words = len(remaining_text.strip().split())

    return non_english_chars + english_words


def preprocess_segments(
    segments: List[ASRDataSeg], need_lower: bool = True
) -> List[ASRDataSeg]:
    """
    预处理ASR数据分段:
    1. 将纯标点符号的分段合并到前一个分段，保留句末边界
    2. 对仅包含字母、数字和撇号的文本进行小写处理并添加空格

    Args:
        segments: ASR数据分段列表
        need_lower: 是否需要转换为小写

    Returns:
        List[ASRDataSeg]: 处理后的分段列表
    """
    new_segments = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue

        if is_pure_punctuation(text):
            if new_segments:
                new_segments[-1].text = f"{new_segments[-1].text.rstrip()}{text}"
                new_segments[-1].end_time = max(new_segments[-1].end_time, seg.end_time)
            continue

        # 如果文本只包含字母、数字和撇号，则将其转换为小写并添加一个空格
        if re.match(r"^[a-zA-Z0-9\']+$", text):
            if need_lower:
                seg.text = text.lower() + " "
            else:
                seg.text = text + " "
        new_segments.append(seg)
    return new_segments


class SubtitleSplitter:
    """字幕分割器，支持缓存功能"""

    PUNCTUATION_SPLIT_SUFFIXES = (
        ".",
        ",",
        "!",
        "?",
        ";",
        ":",
        "。",
        "，",
        "！",
        "？",
        "；",
        "：",
        "、",
    )
    STRONG_TERMINAL_SUFFIXES = (".", "!", "?", "。", "！", "？")
    TERMINAL_STRIP_CHARS = ' \t\r\n"\'”’）)]}】》'
    TITLE_ABBREVIATIONS = frozenset({"mr", "mrs"})
    ENGLISH_PREFIX_SPLIT_WORDS = {
        "and",
        "or",
        "but",
        "if",
        "then",
        "because",
        "as",
        "until",
        "while",
        "when",
        "where",
        "so",
        "however",
        "therefore",
        "although",
        "though",
        "which",
        "that",
        "who",
    }
    CJK_PREFIX_SPLIT_WORDS = (
        "但是",
        "所以",
        "然后",
        "因为",
        "如果",
        "而且",
        "不过",
        "同时",
        "接着",
        "另外",
        "最后",
        "首先",
        "其次",
        "也就是",
        "或者",
        "以及",
        "并且",
        "虽然",
        "因此",
        "那么",
        "其实",
    )

    def __init__(
        self,
        thread_num: int = 5,
        model: str = "gpt-4o-mini",
        temperature: float = 0.4,
        timeout: int = 300,
        retry_times: int = 1,
        split_type: str = "semantic",
        max_word_count_cjk: int = MAX_WORD_COUNT_CJK,
        max_word_count_english: int = MAX_WORD_COUNT_ENGLISH,
        use_cache: bool = False,
        usage_callback: Optional[Callable] = None,
    ):
        """
        初始化字幕分割器

        Args:
            thread_num: 并行处理的线程数
            model: 使用的LLM模型名称
            temperature: LLM温度参数
            timeout: API超时时间（秒）
            retry_times: 重试次数
            split_type: 分段类型，可选值："semantic"（语义分段）或"sentence"（句子分段）
            max_word_count_cjk: 中日韩文本最大字数
            max_word_count_english: 英文文本最大单词数
            use_cache: 是否使用缓存
        """
        self._init_client()
        self.thread_num = thread_num
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.retry_times = retry_times
        self.split_type = split_type
        self.max_word_count_cjk = max_word_count_cjk
        self.max_word_count_english = max_word_count_english
        self.use_cache = use_cache
        self.usage_callback = usage_callback
        self.is_running = True
        self.executor = None
        self.cache_manager = CacheManager(str(CACHE_PATH))

        # 验证分段类型
        if split_type not in ["semantic", "sentence"]:
            raise ValueError(
                f"无效的分段类型: {split_type}，必须是 'semantic' 或 'sentence'"
            )

    @staticmethod
    def _join_segment_texts(segments: List[ASRDataSeg]) -> str:
        raw_text = "".join(seg.text for seg in segments).strip()
        if is_mainly_cjk(raw_text):
            return raw_text
        return " ".join(seg.text.strip() for seg in segments if seg.text.strip())

    @classmethod
    def _ends_with_title_abbreviation(cls, text: str) -> bool:
        stripped = (text or "").rstrip(cls.TERMINAL_STRIP_CHARS)
        match = re.search(r"(?<![A-Za-z])([A-Za-z]+)\.?$", stripped)
        return bool(match and match.group(1).casefold() in cls.TITLE_ABBREVIATIONS)

    @classmethod
    def _is_protected_title_boundary(
        cls, text: str, next_text: str = ""
    ) -> bool:
        return bool((next_text or "").strip()) and cls._ends_with_title_abbreviation(
            text
        )

    @classmethod
    def _has_punctuation_split_boundary(
        cls, text: str, next_text: str = ""
    ) -> bool:
        stripped = (text or "").rstrip(cls.TERMINAL_STRIP_CHARS)
        return not cls._is_protected_title_boundary(
            stripped, next_text
        ) and stripped.endswith(cls.PUNCTUATION_SPLIT_SUFFIXES)

    @classmethod
    def _has_strong_terminal_boundary(cls, text: str, next_text: str = "") -> bool:
        stripped = (text or "").rstrip(cls.TERMINAL_STRIP_CHARS)
        return not cls._is_protected_title_boundary(
            stripped, next_text
        ) and stripped.endswith(cls.STRONG_TERMINAL_SUFFIXES)

    @classmethod
    def _nearest_safe_split_index(
        cls, segments: List[ASRDataSeg], target_index: int
    ) -> int:
        candidates = [
            index
            for index in range(len(segments) - 1)
            if not cls._is_protected_title_boundary(
                segments[index].text, segments[index + 1].text
            )
        ]
        if not candidates:
            return min(max(target_index, 0), len(segments) - 2)
        return min(
            candidates,
            key=lambda index: (
                abs(index - target_index),
                index < target_index,
            ),
        )

    @classmethod
    def _merge_title_boundaries_added_by_split_stage(
        cls, split_segments: List[str], restored_sentences: List[str]
    ) -> List[str]:
        """只撤销切短阶段新增的称谓与姓名边界，保留恢复阶段的句界。"""
        restored_boundary_counts: set[int] = set()
        restored_count = 0
        for index, sentence in enumerate(restored_sentences):
            restored_count += count_words(sentence)
            if index < len(restored_sentences) - 1:
                restored_boundary_counts.add(restored_count)

        merged: List[str] = []
        split_count = 0
        for segment in split_segments:
            segment = segment.strip()
            if not segment:
                continue
            if (
                merged
                and split_count not in restored_boundary_counts
                and cls._is_protected_title_boundary(merged[-1], segment)
            ):
                merged[-1] = f"{merged[-1].rstrip()} {segment}"
            else:
                merged.append(segment)
            split_count += count_words(segment)
        return merged

    def _init_client(self):
        """初始化OpenAI客户端"""
        base_url = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY")
        if not (base_url and api_key):
            raise ValueError("环境变量 OPENAI_BASE_URL 和 OPENAI_API_KEY 必须设置")

        self.client = OpenAI(base_url=base_url, api_key=api_key)

    def _init_thread_pool(self, task_count: int):
        """初始化线程池"""
        max_workers = max(1, min(int(self.thread_num or 1), max(1, task_count)))
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        import atexit

        atexit.register(self.stop)

    def split_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """
        分割字幕文件

        Args:
            subtitle_file: 字幕文件路径

        Returns:
            处理后的ASR数据对象
        """
        try:
            # 读取字幕文件
            if isinstance(subtitle_data, str):
                asr_data = ASRData.from_subtitle_file(subtitle_data)
            else:
                asr_data = subtitle_data

            if not asr_data.is_word_timestamp():
                asr_data = asr_data.split_to_word_segments()
            else:
                asr_data.remove_repeated_asr_artifacts()

            # 预处理ASR数据
            asr_data.segments = preprocess_segments(asr_data.segments, need_lower=False)
            txt = asr_data.to_txt().replace("\n", "")

            # 确定分段数
            total_word_count = count_words(txt)
            num_segments = self._determine_num_segments(total_word_count)
            logger.info(f"根据字数 {total_word_count}，确定分段数: {num_segments}")

            # 分割ASR数据
            asr_data_list = self._split_asr_data(asr_data, num_segments)

            # 多线程处理每个asr_data
            processed_segments = self._process_segments(asr_data_list)

            # 合并所有处理后的分段
            final_segments = self._merge_processed_segments(processed_segments)

            # 对短句进行合并优化
            self.merge_short_segment(final_segments)

            # 补齐断句后相邻字幕之间的短显示空隙
            self._fill_short_display_gaps(final_segments)

            return ASRData(final_segments)

        except Exception as e:
            logger.error(f"分割失败：{str(e)}")
            raise RuntimeError(f"分割失败：{str(e)}")

    def _determine_num_segments(self, word_count: int, threshold: int = 500) -> int:
        """
        根据字数确定分段数

        Args:
            word_count: 总字数
            threshold: 每段的目标字数

        Returns:
            分段数
        """
        num_segments = word_count // threshold
        if word_count % threshold > 0:
            num_segments += 1
        return max(1, num_segments)

    def _split_asr_data(self, asr_data: ASRData, num_segments: int) -> List[ASRData]:
        """
        长文本发送LLM前进行进行分割，根据ASR分段中的时间间隔，将ASRData拆分成多个部分。

        处理步骤：
        1. 计算总字数，并确定每个分段的字数范围。
        2. 确定平均分割点。
        3. 在分割点前后一定范围内，寻找时间间隔最大的点作为实际的分割点。

        Args:
            asr_data: ASR数据对象
            num_segments: 目标分段数

        Returns:
            ASR数据对象列表
        """
        SPLIT_RANGE = 30  # 在分割点前后寻找最大时间间隔的范围

        total_segs = len(asr_data.segments)
        total_word_count = count_words(asr_data.to_txt())
        words_per_segment = total_word_count // num_segments

        if num_segments <= 1 or total_segs <= num_segments:
            return [asr_data]

        # 计算每个分段的大致字数 根据每段字数计算分割点
        split_indices = [i * words_per_segment for i in range(1, num_segments)]

        # 调整分割点：在每个平均分割点附近寻找时间间隔最大的点
        adjusted_split_indices = []
        for split_point in split_indices:
            # 定义搜索范围
            start = max(0, split_point - SPLIT_RANGE)
            end = min(total_segs - 1, split_point + SPLIT_RANGE)

            max_gap = -1
            best_index = None
            for index in range(start, end):
                if self._is_protected_title_boundary(
                    asr_data.segments[index].text,
                    asr_data.segments[index + 1].text,
                ):
                    continue
                gap = (
                    asr_data.segments[index + 1].start_time
                    - asr_data.segments[index].end_time
                )
                if gap > max_gap:
                    max_gap = gap
                    best_index = index

            if best_index is None:
                best_index = self._nearest_safe_split_index(
                    asr_data.segments, split_point
                )

            adjusted_split_indices.append(best_index)

        # 移除重复的分割点
        adjusted_split_indices = sorted(list(set(adjusted_split_indices)))

        # 根据调整后的分割点拆分ASRData
        segments = []
        prev_index = 0
        for index in adjusted_split_indices:
            part = ASRData(asr_data.segments[prev_index : index + 1])
            segments.append(part)
            prev_index = index + 1

        # 添加最后一部分
        if prev_index < total_segs:
            part = ASRData(asr_data.segments[prev_index:])
            segments.append(part)

        return segments

    def _process_segments(self, asr_data_list: List[ASRData]) -> List[List[ASRDataSeg]]:
        """并行处理所有分段"""
        if not asr_data_list:
            return []
        self._init_thread_pool(len(asr_data_list))
        futures = []
        for asr_data in asr_data_list:
            if not self.executor:
                raise ValueError("线程池未初始化")
            future = self.executor.submit(self._process_single_segment, asr_data)
            futures.append(future)

        processed_segments = []
        for future in as_completed(futures):
            if not self.is_running:
                logger.info("处理被中断，退出处理")
                break
            try:
                result = future.result()
                processed_segments.append(result)
            except Exception as e:
                logger.error(f"处理分段失败：{str(e)}")

        return processed_segments

    def _process_single_segment(self, asr_data_part: ASRData) -> List[ASRDataSeg]:
        """
        处理单个分段
        """
        if not asr_data_part.segments:
            return []
        for i in range(self.retry_times):
            try:
                return self._process_by_llm(asr_data_part.segments)
            except Exception as e:
                if i == self.retry_times - 1:
                    logger.warning(
                        f"LLM处理失败，使用规则based方法进行分割: {format_openai_compat_error(e, model_name=self.model)}"
                    )
                    return self._process_by_rules(asr_data_part.segments)
                logger.warning(
                    f"分割重试 {i+1}/{self.retry_times}: {format_openai_compat_error(e, model_name=self.model)}"
                )
        return self._process_by_rules(asr_data_part.segments)  # 确保总是有返回值

    def _process_by_llm(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """
        使用LLM进行分段处理。

        词级时间轴文本通常没有标点和句子边界，单次让AI直接切短字幕容易误判。
        这里先恢复完整句子，再把完整句子交给断句提示词做字幕长度分句。
        """
        txt = self._join_segment_texts(segments)
        logger.debug(f"处理文本长度: {len(txt)}")

        restored_sentences = self._restore_sentences_with_llm(txt)
        split_sentences = self._split_restored_sentences_with_llm(
            txt, restored_sentences
        )

        result = self._merge_segments_based_on_sentences(segments, split_sentences)
        if not self._has_same_lexical_content(segments, result):
            logger.warning("LLM断句未完整覆盖原始词，回退到无损规则分割")
            return self._process_by_rules(segments)
        return result

    @staticmethod
    def _lexical_tokens(segments: List[ASRDataSeg]) -> List[str]:
        text = " ".join((segment.text or "").strip() for segment in segments)
        return re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text.lower(), flags=re.UNICODE)

    @classmethod
    def _has_same_lexical_content(
        cls, source: List[ASRDataSeg], result: List[ASRDataSeg]
    ) -> bool:
        return cls._lexical_tokens(source) == cls._lexical_tokens(result)

    def _get_split_prompt_template(self) -> Template:
        if self.split_type == "semantic":
            return Template(get_prompt_template(PROMPT_SPLIT_SEMANTIC))
        if self.split_type == "sentence":
            return Template(get_prompt_template(PROMPT_SPLIT_SENTENCE))
        raise ValueError(f"无效的分段类型: {self.split_type}")

    def _call_split_llm(
        self,
        *,
        stage: str,
        cache_key: str,
        source_text: str,
        system_prompt: str,
        user_prompt: str,
        cache_extra: Optional[dict] = None,
    ) -> List[str]:
        param = {
            "temperature": self.temperature,
            "split_stage": stage,
            "prompt_hash": hashlib.md5(system_prompt.encode("utf-8")).hexdigest(),
            "split_strategy_version": SPLIT_STRATEGY_VERSION,
        }
        if cache_extra:
            param.update(cache_extra)

        if self.use_cache:
            cached_result = self.cache_manager.get_llm_result(
                prompt=cache_key,
                model_name=self.model,
                **param,
            )
            if cached_result:
                try:
                    logger.info(
                        f"使用缓存数据进行{stage}，文本长度: {count_words(source_text)}"
                    )
                    cached_segments = json.loads(cached_result)
                    if cached_segments:
                        return cached_segments
                except json.JSONDecodeError as e:
                    logger.warning(f"缓存数据解析失败: {str(e)}")

        logger.info(f"开始调用API进行{stage}，文本长度: {count_words(source_text)}")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            **get_openai_compat_request_options(
                model_name=self.model, default_timeout=self.timeout
            ),
        )
        if self.usage_callback:
            self.usage_callback(stage, extract_openai_usage(response))

        result = response.choices[0].message.content
        if result is None:
            raise ValueError("API返回的内容为空")

        result = result.strip()
        if "<br" not in result.lower():
            result = re.sub(r"\n+", "<br>", result)
        result = re.sub(r"\s*<br\s*/?>\s*", "<br>", result, flags=re.I)
        result = result.replace("\n", "")
        result_segments = [
            segment.strip() for segment in result.split("<br>") if segment.strip()
        ]

        if not result_segments:
            raise ValueError(f"API返回的{stage}结果为空")
        source_units = count_words(source_text)
        if stage == "split" and len(result_segments) > max(200, source_units * 2):
            raise ValueError(
                f"API返回的{stage}结果异常过多: {len(result_segments)}，"
                f"源文本长度: {source_units}"
            )

        logger.info(f"API返回{stage}结果，句子数量: {len(result_segments)}")

        if self.use_cache:
            try:
                self.cache_manager.set_llm_result(
                    prompt=cache_key,
                    result=json.dumps(result_segments, ensure_ascii=False),
                    model_name=self.model,
                    **param,
                )
            except Exception as e:
                logger.error(f"写入缓存失败: {str(e)}")

        return result_segments

    def _restore_sentences_with_llm(self, txt: str) -> List[str]:
        system_prompt = get_prompt_template(PROMPT_SPLIT_SENTENCE_RESTORE)
        user_prompt = (
            "Please restore complete sentences for the following word-level "
            f"subtitle text, using <br> only between complete sentences:\n{txt}"
        )
        return self._call_split_llm(
            stage="split_sentence_restore",
            cache_key=txt,
            source_text=txt,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def _split_restored_sentences_with_llm(
        self, original_text: str, restored_sentences: List[str]
    ) -> List[str]:
        template = self._get_split_prompt_template()
        system_prompt = template.safe_substitute(
            max_word_count_cjk=self.max_word_count_cjk,
            max_word_count_english=self.max_word_count_english,
        )
        restored_text = "\n".join(restored_sentences)
        user_prompt = (
            "Please split the following restored complete sentences into subtitle "
            "segments with <br>. Keep the text order and do not translate or "
            f"rewrite the content:\n{restored_text}"
        )

        split_segments = self._call_split_llm(
            stage="split",
            cache_key=original_text,
            source_text=restored_text,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            cache_extra={
                "split_type": self.split_type,
                "max_word_count_cjk": self.max_word_count_cjk,
                "max_word_count_english": self.max_word_count_english,
                "restore_hash": hashlib.md5(
                    restored_text.encode("utf-8")
                ).hexdigest(),
            },
        )
        return self._merge_title_boundaries_added_by_split_stage(
            split_segments, restored_sentences
        )

    def _process_by_rules(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """
        使用规则进行基础的句子分割

        规则包括:
        1. 考虑时间间隔，超过阈值的进行分割
        2. 在常见连接词前后进行分割（保证分割后两个分段都大于5个单词）
        3. 分割大于 MAX_WORD_COUNT 个单词的分段

        Args:
            segments: ASR数据分段列表
        Returns:
            处理后的分段列表
        """
        logger.info(f"分段: {len(segments)}")
        # 1. 先按时间间隔分组
        segment_groups = self._group_by_time_gaps(
            segments, max_gap=500, check_large_gaps=True
        )
        logger.info(f"按时间间隔分组分组: {len(segment_groups)}")

        # 2. 按常用词分割, 只处理长句
        common_result_groups = []
        for group in segment_groups:
            for strict_group in self._split_by_strong_terminal_punctuation(group):
                group_text = self._join_segment_texts(strict_group)
                max_word_count = (
                    self.max_word_count_cjk
                    if is_mainly_cjk(group_text)
                    else self.max_word_count_english
                )
                if count_words(group_text) > max_word_count:
                    split_groups = self._split_by_common_words(strict_group)
                    common_result_groups.extend(split_groups)
                else:
                    common_result_groups.append(strict_group)

        # 3. 处理过长的分段，并合并group为seg
        result_segments = []
        for group in common_result_groups:
            result_segments.extend(self._split_long_segment(group))

        return result_segments

    def _group_by_time_gaps(
        self,
        segments: List[ASRDataSeg],
        max_gap: int = MAX_GAP,
        check_large_gaps: bool = False,
    ) -> List[List[ASRDataSeg]]:
        """
        检查字幕分段之间的时间间隔，如果超过阈值则分段

        Args:
            segments: 待检查的分段列表
            max_gap: 最大允许的时间间隔（ms）
            check_large_gaps: 是否检查连续的大时间间隔

        Returns:
            分段后的列表的列表
        """
        if not segments:
            return []

        result = []
        current_group = [segments[0]]
        recent_gaps = []  # 存储最近的时间间隔
        WINDOW_SIZE = 5  # 检查最近5个间隔

        for i in range(1, len(segments)):
            time_gap = segments[i].start_time - segments[i - 1].end_time
            protected_title_boundary = (
                self._is_protected_title_boundary(
                    segments[i - 1].text, segments[i].text
                )
            )

            if check_large_gaps and not protected_title_boundary:
                recent_gaps.append(time_gap)
                if len(recent_gaps) > WINDOW_SIZE:
                    recent_gaps.pop(0)
                if len(recent_gaps) == WINDOW_SIZE:
                    avg_gap = sum(recent_gaps) / len(recent_gaps)
                    # 如果当前间隔大于平均值的3倍
                    if time_gap > avg_gap * 3 and len(current_group) > 5:
                        # logger.debug(
                        #     f"检测到大间隔: {time_gap}ms, 平均间隔: {avg_gap}ms"
                        # )
                        result.append(current_group)
                        current_group = []
                        recent_gaps = []  # 重置间隔记录

            if time_gap > max_gap and not protected_title_boundary:
                # logger.debug(
                #     f"超过阈值，分组: {''.join(seg.text for seg in current_group)}"
                # )
                result.append(current_group)
                current_group = []
                recent_gaps = []  # 重置间隔记录

            current_group.append(segments[i])

        if current_group:
            result.append(current_group)

        return result

    def _split_by_strong_terminal_punctuation(
        self, segments: List[ASRDataSeg]
    ) -> List[List[ASRDataSeg]]:
        """按句末强终止标点切分，避免问号等边界被长度阈值吞掉。"""
        if not segments:
            return []

        result = []
        current_group = []
        for index, seg in enumerate(segments):
            current_group.append(seg)
            if (
                index < len(segments) - 1
                and self._has_strong_terminal_boundary(
                    seg.text, segments[index + 1].text
                )
            ):
                result.append(current_group)
                logger.debug(f"在强终止标点后分割: {seg.text}")
                current_group = []

        if current_group:
            result.append(current_group)
        return result

    def _split_by_common_words(
        self, segments: List[ASRDataSeg]
    ) -> List[List[ASRDataSeg]]:
        """
        在常见连接词前后进行分割，确保分割后的每个分段都至少有5个单词

        Args:
            segments: ASR数据分段列表
        Returns:
            分组后的分段列表的列表
        """
        # 定义在词语前面分割的常见词
        prefix_split_words = {
            # 英文连接词和介词
            "and",
            "or",
            "but",
            "if",
            "then",
            "because",
            "as",
            "until",
            "while",
            "what",
            "when",
            "where",
            "nor",
            "yet",
            "so",
            "for",
            "however",
            "moreover",
            # 中文连接词
            "和",
            "及",
            "与",
            "但",
            "而",
            "或",
            "因",
            # 中文代词
            "我",
            "你",
            "他",
            "她",
            "它",
            "咱",
            "您",
            "这",
            "那",
            "哪",
        }

        # 定义在词语后面分割的常见词
        suffix_split_words = {
            # 标点符号
            ".",
            ",",
            "!",
            "?",
            "。",
            "，",
            "！",
            "？",
            # 中文语气词和助词
            "的",
            "了",
            "着",
            "过",
            "吗",
            "呢",
            "吧",
            "啊",
            "呀",
            "嘛",
            "啦",
            # 英文所有格代词
            "mine",
            "yours",
            "hers",
            "its",
            "ours",
            "theirs",
            # 英文副词
            "either",
            "neither",
        }

        result = []
        current_group = []

        for i, seg in enumerate(segments):
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(seg.text)
                else self.max_word_count_english
            )

            # 强终止标点是硬边界，不受最小长度阈值限制
            if (
                i > 0
                and current_group
                and self._has_strong_terminal_boundary(
                    segments[i - 1].text, seg.text
                )
            ):
                result.append(current_group)
                logger.debug(f"在强终止标点 {segments[i-1].text} 后分割")
                current_group = []

            # 如果当前词是前缀词且前面已经累积了足够多的词
            if any(
                seg.text.lower().startswith(word) for word in prefix_split_words
            ) and current_group and len(current_group) >= int(max_word_count * 0.6):
                result.append(current_group)
                logger.debug(f"在前缀词 {seg.text} 前分割")
                current_group = []

            # 如果前一个词是后缀词且当前组至少有一定数量的词
            if (
                i > 0
                and any(
                    segments[i - 1].text.lower().endswith(word)
                    for word in suffix_split_words
                )
                and not self._is_protected_title_boundary(
                    segments[i - 1].text, seg.text
                )
                and len(current_group) >= int(max_word_count * 0.4)
            ):
                result.append(current_group)
                logger.debug(f"在后缀词 {segments[i-1].text} 后分割")
                current_group = []

            current_group.append(seg)

        # 处理最后剩余的分组
        if current_group:
            result.append(current_group)

        return result

    def _split_long_segment(
        self, segments: List[ASRDataSeg], hint_text: str = ""
    ) -> List[ASRDataSeg]:
        """
        基于最大时间间隔拆分长分段，根据文本类型使用不同的最大词数限制

        Args:
            segments: 要拆分的分段列表
        Returns:
            拆分后的分段列表
        """
        result_segs = []
        segments_to_process = [(segments, hint_text)]  # 使用列表作为处理队列

        while segments_to_process:
            current_segments, current_hint_text = segments_to_process.pop(0)

            # 添加空列表检查
            if not current_segments:
                continue

            strict_groups = self._split_by_strong_terminal_punctuation(
                current_segments
            )
            if len(strict_groups) > 1:
                segments_to_process = [
                    (group, "") for group in strict_groups
                ] + segments_to_process
                continue

            hint_groups = self._split_by_hint_terminal_boundaries(
                current_segments, current_hint_text
            )
            if len(hint_groups) > 1:
                segments_to_process = [
                    (group, "") for group in hint_groups
                ] + segments_to_process
                continue

            merged_text = self._join_segment_texts(current_segments)
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(merged_text)
                else self.max_word_count_english
            )
            n = len(current_segments)

            # 基本情况：如果分段足够短或无法进一步拆分
            if count_words(merged_text) <= max_word_count or n < 4:
                merged_seg = ASRDataSeg(
                    merged_text.strip(),
                    current_segments[0].start_time,
                    current_segments[-1].end_time,
                )
                result_segs.append(merged_seg)
                continue

            natural_split_index = self._find_natural_split_index(
                current_segments, max_word_count, current_hint_text
            )
            if natural_split_index is not None:
                split_index = natural_split_index
            else:
                # 检查时间间隔是否都相等
                gaps = [
                    current_segments[i + 1].start_time - current_segments[i].end_time
                    for i in range(n - 1)
                ]
                all_equal = all(abs(gap - gaps[0]) < 1e-6 for gap in gaps)

                if all_equal:
                    # 如果时间间隔都相等，在中间位置断句
                    split_index = self._nearest_safe_split_index(
                        current_segments, n // 2
                    )
                else:
                    # 在分段中间2/3部分寻找最大时间间隔点
                    start_idx = max(n // 6, 1)
                    end_idx = min((5 * n) // 6, n - 2)
                    candidate_indices = [
                        index
                        for index in range(start_idx, end_idx)
                        if not self._is_protected_title_boundary(
                            current_segments[index].text,
                            current_segments[index + 1].text,
                        )
                    ]
                    if candidate_indices:
                        split_index = max(
                            candidate_indices,
                            key=lambda index: current_segments[
                                index + 1
                            ].start_time
                            - current_segments[index].end_time,
                        )
                    else:
                        split_index = self._nearest_safe_split_index(
                            current_segments, n // 2
                        )
                    if split_index == 0 or split_index == n - 1:
                        split_index = self._nearest_safe_split_index(
                            current_segments, n // 2
                        )

            # 将分割后的两部分添加到处理队列中
            first_segs = current_segments[: split_index + 1]
            second_segs = current_segments[split_index + 1 :]

            segments_to_process.extend([(first_segs, ""), (second_segs, "")])

        # 按时间排序
        result_segs.sort(key=lambda seg: seg.start_time)
        return result_segs

    def _find_natural_split_index(
        self,
        segments: List[ASRDataSeg],
        max_word_count: int,
        hint_text: str = "",
    ) -> Optional[int]:
        """
        优先在标点或连接词边界拆分长字幕。

        返回值是左侧分组的最后一个索引；None 表示没有合适自然断点。
        """
        if len(segments) < 2:
            return None

        full_text = self._join_segment_texts(segments)
        is_cjk_text = is_mainly_cjk(full_text)
        min_left_count = max(2 if is_cjk_text else 3, int(max_word_count * 0.35))
        target_count = max(min_left_count, int(max_word_count * 0.8))
        candidates: list[tuple[int, int, int]] = []
        hint_boundary_counts = self._hint_boundary_counts(hint_text, is_cjk_text)

        for index in range(len(segments) - 1):
            left = segments[: index + 1]
            right = segments[index + 1 :]
            left_text = self._join_segment_texts(left)
            right_text = self._join_segment_texts(right)
            left_count = count_words(left_text)
            right_count = count_words(right_text)
            current_text = segments[index].text.strip()
            next_text = segments[index + 1].text.strip()
            if (
                self._has_strong_terminal_boundary(current_text, next_text)
                and right_count > 0
            ):
                return index

            if (
                left_count < min_left_count
                or left_count > max_word_count
                or right_count <= 0
            ):
                continue

            if left_count in hint_boundary_counts:
                candidates.append((0, abs(left_count - target_count), index))
                continue

            if self._has_punctuation_split_boundary(current_text, next_text):
                candidates.append((1, abs(left_count - target_count), index))
                continue

            if is_cjk_text:
                if any(
                    right_text.startswith(word)
                    for word in self.CJK_PREFIX_SPLIT_WORDS
                ):
                    candidates.append((2, abs(left_count - target_count), index))
                continue

            normalized_next = next_text.lower().strip(" \t\r\n.,!?;:\"'()[]{}")
            if normalized_next in self.ENGLISH_PREFIX_SPLIT_WORDS:
                candidates.append((2, abs(left_count - target_count), index))

        if not candidates:
            return None

        candidates.sort()
        return candidates[0][2]

    def _split_by_hint_terminal_boundaries(
        self, segments: List[ASRDataSeg], hint_text: str = ""
    ) -> List[List[ASRDataSeg]]:
        """按 LLM 恢复文本中的强终止标点边界切回原始时间轴。"""
        if not hint_text or len(segments) < 2:
            return [segments]

        is_cjk_text = is_mainly_cjk(self._join_segment_texts(segments))
        boundaries = self._hint_terminal_boundary_counts(hint_text, is_cjk_text)
        if not boundaries:
            return [segments]

        result = []
        current_group = []
        cumulative_count = 0
        for index, seg in enumerate(segments):
            current_group.append(seg)
            cumulative_count += count_words(seg.text)
            if index < len(segments) - 1 and cumulative_count in boundaries:
                result.append(current_group)
                logger.debug("按恢复文本强终止标点边界分割: %s", cumulative_count)
                current_group = []

        if current_group:
            result.append(current_group)
        return result or [segments]

    def _hint_terminal_boundary_counts(
        self, hint_text: str, is_cjk_text: bool
    ) -> set[int]:
        """只从 LLM 恢复文本里的强终止标点提取硬边界。"""
        boundaries: set[int] = set()
        if not hint_text:
            return boundaries

        if is_cjk_text:
            visible_count = 0
            for index, char in enumerate(hint_text):
                if char in self.STRONG_TERMINAL_SUFFIXES:
                    if self._is_protected_title_boundary(
                        hint_text[: index + 1], hint_text[index + 1 :]
                    ):
                        continue
                    if visible_count > 0:
                        boundaries.add(visible_count)
                    continue
                if char in self.PUNCTUATION_SPLIT_SUFFIXES or char.isspace():
                    continue
                visible_count += 1
            return boundaries

        words = [word for word in hint_text.split() if word.strip()]
        for index, word in enumerate(words):
            next_word = words[index + 1] if index + 1 < len(words) else ""
            if self._has_strong_terminal_boundary(word, next_word):
                boundaries.add(index + 1)
        return boundaries

    def _hint_boundary_counts(self, hint_text: str, is_cjk_text: bool) -> set[int]:
        """从 LLM 返回文本中的标点和连接词推断建议断点位置。"""
        if not hint_text:
            return set()

        boundaries: set[int] = set()
        if is_cjk_text:
            visible_count = 0
            for index, char in enumerate(hint_text):
                if char in self.PUNCTUATION_SPLIT_SUFFIXES:
                    if self._is_protected_title_boundary(
                        hint_text[: index + 1], hint_text[index + 1 :]
                    ):
                        continue
                    if visible_count > 0:
                        boundaries.add(visible_count)
                    continue
                if not char.isspace():
                    visible_count += 1

            clean_text = re.sub(
                "[" + re.escape("".join(self.PUNCTUATION_SPLIT_SUFFIXES)) + r"\s]+",
                "",
                hint_text,
            )
            for word in self.CJK_PREFIX_SPLIT_WORDS:
                start = clean_text.find(word)
                if start > 0:
                    boundaries.add(start)
            return boundaries

        words = [word for word in hint_text.split() if word.strip()]
        for index, word in enumerate(words):
            stripped = word.strip()
            normalized = stripped.lower().strip(" \t\r\n.,!?;:\"'()[]{}")
            next_word = words[index + 1] if index + 1 < len(words) else ""
            if self._has_punctuation_split_boundary(stripped, next_word):
                boundaries.add(index + 1)
            if normalized in self.ENGLISH_PREFIX_SPLIT_WORDS and index > 0:
                boundaries.add(index)
        return boundaries

    def _merge_processed_segments(
        self, processed_segments: List[List[ASRDataSeg]]
    ) -> List[ASRDataSeg]:
        """
        合并处理后的分段

        Args:
            processed_segments: 处理后的分段列表的列表

        Returns:
            最终的分段列表
        """
        final_segments = []
        for segments in processed_segments:
            final_segments.extend(segments)

        # 按时间排序
        final_segments.sort(key=lambda seg: seg.start_time)
        return final_segments

    def merge_short_segment(self, segments: List[ASRDataSeg]) -> None:
        """
        经过LLM断句后，继续优化字幕，合并词数少于等于5且时间相邻的段落。

        Args:
            segments: 字幕段落列表，将直接在此列表上进行修改
        """
        if not segments:  # 添加空列表检查
            return

        i = 0  # 从头开始遍历
        while i < len(segments) - 1:  # 修改遍历方式
            current_seg = segments[i]
            next_seg = segments[i + 1]

            # 判断是否需要合并:
            # 1. 时间间隔小于300ms
            # 2. 当前段落或下一段落词数小于 4
            # 3. 合并后总词数不超过限制
            time_gap = abs(next_seg.start_time - current_seg.end_time)
            current_words = count_words(current_seg.text)
            next_words = count_words(next_seg.text)
            total_words = current_words + next_words
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(current_seg.text)
                else self.max_word_count_english
            )

            if self._has_strong_terminal_boundary(
                current_seg.text, next_seg.text
            ):
                i += 1
                continue

            if (
                time_gap < 200
                and (current_words < 5 or next_words < 5)
                and total_words <= max_word_count
            ) or (
                time_gap < 500
                and (current_words < 3 or next_words < 3)
                and total_words <= max_word_count
            ):
                # 执行合并操作
                logger.debug(
                    f"优化：合并相邻分段: {current_seg.text} --- {next_seg.text} -> {time_gap}"
                )

                # 更新当前段落的文本和结束时间
                if is_mainly_cjk(current_seg.text):
                    current_seg.text += next_seg.text  # 直接连接
                else:
                    current_seg.text += " " + next_seg.text  # 加空格连接
                current_seg.end_time = next_seg.end_time

                # 从列表中移除下一个段落
                segments.pop(i + 1)
                # 不增加i，因为需要继续检查合并后的段落
            else:
                i += 1

    @staticmethod
    def _fill_short_display_gaps(
        segments: List[ASRDataSeg], max_gap_ms: int = SHORT_DISPLAY_GAP_FILL_MS
    ) -> None:
        """将断句后的短显示空隙补到下一句开始时间。"""
        for index in range(len(segments) - 1):
            current_seg = segments[index]
            next_seg = segments[index + 1]
            time_gap = next_seg.start_time - current_seg.end_time
            if 0 < time_gap <= max_gap_ms:
                current_seg.end_time = next_seg.start_time

    def _merge_segments_based_on_sentences(
        self, segments: List[ASRDataSeg], sentences: List[str], max_unmatched: int = 5
    ) -> List[ASRDataSeg]:
        """
        基于提供的句子列表合并ASR分段

        Args:
            segments: ASR数据分段列表
            sentences: 句子列表
            max_unmatched: 允许的最大未匹配句子数量

        Returns:
            List[ASRDataSeg]: 合并后的分段列表

        Raises:
            ValueError: 当未匹配句子数量超过阈值时抛出
        """

        def preprocess_text(s: str) -> str:
            """标准化文本，匹配时忽略AI成句阶段补充的标点。"""
            text = re.sub(r"<br\s*/?>", " ", s, flags=re.I).lower()
            punctuation = "".join(self.PUNCTUATION_SPLIT_SUFFIXES)
            text = re.sub(
                r"["
                + re.escape(punctuation)
                + r"\"'“”‘’（）()【】\[\]《》<>]+",
                " ",
                text,
            )
            if is_mainly_cjk(text):
                return "".join(text.split())
            return " ".join(text.split())

        asr_texts = [seg.text for seg in segments]
        asr_len = len(asr_texts)
        asr_index = 0  # 当前分段索引位置
        threshold = 0.5  # 相似度阈值
        max_shift = 30  # 滑动窗口的最大偏移量
        unmatched_count = 0  # 未匹配句子计数

        new_segments = []

        for sentence in sentences:
            logger.debug(f"==========")
            logger.debug(f"处理句子: {sentence}")
            logger.debug("后续句子:" + "".join(asr_texts[asr_index : asr_index + 10]))

            sentence_proc = preprocess_text(sentence)
            word_count = count_words(sentence_proc)
            best_ratio = 0.0
            best_pos = None
            best_window_size = 0

            # 滑动窗口大小，优先考虑接近句子词数的窗口
            max_window_size = min(word_count * 2, asr_len - asr_index)
            min_window_size = max(1, word_count // 2)
            window_sizes = sorted(
                range(min_window_size, max_window_size + 1),
                key=lambda x: abs(x - word_count),
            )

            # 对每个窗口大小尝试匹配
            for window_size in window_sizes:
                max_start = min(asr_index + max_shift + 1, asr_len - window_size + 1)
                for start in range(asr_index, max_start):
                    substr = self._join_segment_texts(
                        segments[start : start + window_size]
                    )
                    substr_proc = preprocess_text(substr)
                    ratio = difflib.SequenceMatcher(
                        None, sentence_proc, substr_proc
                    ).ratio()

                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_pos = start
                        best_window_size = window_size
                    if ratio == 1.0:
                        break
                if best_ratio == 1.0:
                    break

            # 处理匹配结果
            if best_ratio >= threshold and best_pos is not None:
                # Never discard source words merely because a fuzzy match starts
                # later. Include the unconsumed prefix in the current result.
                start_seg_index = asr_index
                end_seg_index = best_pos + best_window_size - 1

                segs_to_merge = segments[start_seg_index : end_seg_index + 1]

                # 按照时间切分避免合并跨度大的
                seg_groups = self._group_by_time_gaps(segs_to_merge, max_gap=MAX_GAP)

                for group in seg_groups:
                    merged_text = self._join_segment_texts(group)
                    merged_start_time = group[0].start_time
                    merged_end_time = group[-1].end_time
                    merged_seg = ASRDataSeg(
                        merged_text,
                        merged_start_time,
                        merged_end_time,
                    )

                    logger.debug(f"合并分段: {merged_seg.text}")

                    # 考虑最大词数的拆分
                    split_segs = self._split_long_segment(group, sentence)
                    new_segments.extend(split_segs)
                max_shift = 30
                asr_index = end_seg_index + 1  # 移动到下一个未处理的分段
            else:
                logger.warning(f"无法匹配句子: {sentence}")
                unmatched_count += 1
                if unmatched_count > max_unmatched:
                    raise ValueError(
                        f"未匹配句子数量超过阈值 {max_unmatched}，处理终止"
                    )
                max_shift = 100
                # An unmatched LLM sentence must not consume source words. A
                # later sentence may still align with the current ASR position.

        if asr_index < asr_len:
            new_segments.extend(self._process_by_rules(segments[asr_index:]))

        return new_segments

    def stop(self):
        """停止分割器"""
        if not self.is_running:
            return

        logger.info("正在停止分割器...")
        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"关闭线程池时出错：{str(e)}")
            finally:
                self.executor = None
