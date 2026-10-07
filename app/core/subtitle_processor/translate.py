import hashlib
import math
from string import Template
from typing import Callable, Dict, Optional, List, Any, Union
import logging
from pathlib import Path
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
from openai import OpenAI
import json
from dataclasses import dataclass
from functools import lru_cache
import signal
import re

from app.config import CACHE_PATH
from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.storage.cache_manager import CacheManager
from app.core.utils import json_repair
from app.core.subtitle_processor.prompt import (
    PROMPT_REFLECT_TRANSLATE,
    PROMPT_SINGLE_TRANSLATE,
    PROMPT_TRANSLATE,
    get_prompt_template,
)
from app.core.utils.logger import setup_logger
from app.core.utils.openai_compat import (
    extract_openai_usage,
    format_openai_compat_error,
    get_openai_compat_request_options,
)
from app.core.utils.transcript_terms import (
    extract_glossary_pairs,
    filter_document_prompt_for_text,
)


logger = setup_logger("subtitle_translator")

TRANSLATION_READABILITY_POLICY_VERSION = 1
REFLECT_POSTPROCESS_POLICY_VERSION = 1
TRANSLATION_CHUNK_MAX_JSON_CHARS = 6000
CJK_READING_CHARS_PER_SECOND = 12
NON_CJK_READING_WORDS_PER_SECOND = 3
MIN_CJK_READING_BUDGET = 18
MIN_NON_CJK_READING_BUDGET = 6

REFLECT_POSTPROCESS_PROMPT = """
# Role: 字幕反思润色与时间轴分割专家

你需要根据字幕时间轴、原文和现有译文，对译文做最终润色，并在译文过长或阅读负担过高时，将同一条字幕拆成多条更易阅读的字幕。

要求：
- 输出必须是纯 JSON 数组，不要解释，不要 Markdown。
- 必须保留所有输入字幕的语义，不得摘要、删减数字、否定、条件、因果、动作对象、专有名词或术语。
- 可以润色所有译文，使其更自然、连贯、符合${target_language}阅读习惯。
- 只有在更利于阅读时才拆分字幕；不需要拆分时保留一条输出。
- 拆分时必须同步拆分 original_subtitle 与 translated_subtitle，不能只拆译文。
- 每个输出项必须带 source_key，并且 start_time/end_time 必须位于该 source_key 原字幕时间范围内。
- 同一 source_key 拆出的多条字幕必须按时间递增，且 start_time < end_time。
- 同一 source_key 的输出应覆盖原始 start_time 到 end_time，除非原文确有明显停顿或空白。

${length_instruction}

输入 JSON 数组字段：
- source_key: 原字幕编号
- start_time: 开始时间，毫秒
- end_time: 结束时间，毫秒
- duration_ms: 持续时间，毫秒
- original_subtitle: 原文
- translated_subtitle: 当前反思定稿译文
- suggested_length: 建议译文长度，可能为空
- length_unit: 建议长度单位
- is_overlong: 当前译文是否超过建议长度

输出 JSON 数组字段：
- source_key
- start_time
- end_time
- original_subtitle
- translated_subtitle

示例输入：
[
  {
    "source_key": "1",
    "start_time": 1000,
    "end_time": 5000,
    "duration_ms": 4000,
    "original_subtitle": "We need to finish the design today and send it to the client before the meeting.",
    "translated_subtitle": "我们需要今天完成设计，并在会议前发给客户。",
    "suggested_length": 18,
    "length_unit": "字",
    "is_overlong": true
  }
]

示例输出：
[
  {
    "source_key": "1",
    "start_time": 1000,
    "end_time": 3000,
    "original_subtitle": "We need to finish the design today",
    "translated_subtitle": "我们今天要完成设计"
  },
  {
    "source_key": "1",
    "start_time": 3000,
    "end_time": 5000,
    "original_subtitle": "and send it to the client before the meeting.",
    "translated_subtitle": "并在会前发给客户"
  }
]
"""


class BaseTranslator(ABC):
    """翻译器基类"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        retry_times: int = 1,
        timeout: int = 300,
        use_cache: bool = True,
        batch_context_enabled: bool = True,
        batch_context_max_chars: int = 300,
        update_callback: Optional[Callable] = None,
        custom_prompt: Optional[str] = None,
        usage_callback: Optional[Callable] = None,
        status_callback: Optional[Callable] = None,
    ):
        self.thread_num = thread_num
        self.batch_num = batch_num
        self.target_language = target_language
        self.retry_times = retry_times
        self.timeout = timeout
        self.use_cache = use_cache
        self.batch_context_enabled = batch_context_enabled
        self.batch_context_max_chars = max(0, int(batch_context_max_chars or 0))
        self.batch_chunk_max_chars = TRANSLATION_CHUNK_MAX_JSON_CHARS
        self.is_running = True
        self.update_callback = update_callback
        self.custom_prompt = custom_prompt
        self.usage_callback = usage_callback
        self.status_callback = status_callback
        self.cache_manager = CacheManager(str(CACHE_PATH))
        self.executor = None

    def _init_thread_pool(self, task_count: int):
        """初始化线程池"""
        max_workers = max(1, min(int(self.thread_num or 1), max(1, task_count)))
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        import atexit

        atexit.register(self.stop)

    def translate_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """翻译字幕文件"""
        try:
            # 读取字幕文件
            if isinstance(subtitle_data, str):
                asr_data = ASRData.from_subtitle_file(subtitle_data)
            else:
                asr_data = subtitle_data

            # 将ASRData转换为字典格式
            subtitle_dict = {
                str(i): seg.text for i, seg in enumerate(asr_data.segments, 1)
            }
            self._subtitle_timing_by_key = {
                str(i): {
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "duration_ms": max(0, seg.end_time - seg.start_time),
                }
                for i, seg in enumerate(asr_data.segments, 1)
            }

            # 分批处理字幕
            chunks = self._split_chunks(subtitle_dict)

            # 多线程翻译
            translated_dict = self._parallel_translate(chunks)

            # 创建新的ASRDataSeg列表
            new_segments = self._create_segments(asr_data.segments, translated_dict)

            translated_asr_data = ASRData(new_segments)
            return self._postprocess_translated_asr_data(translated_asr_data)
        except Exception as e:
            logger.error(f"翻译失败：{str(e)}")
            raise RuntimeError(f"翻译失败：{str(e)}")

    def _emit_status(self, message: str):
        logger.info(message)
        if self.status_callback:
            self.status_callback(message)

    def _postprocess_translated_asr_data(self, asr_data: ASRData) -> ASRData:
        return asr_data

    def _split_chunks(self, subtitle_dict: Dict[str, str]) -> List[Dict[str, str]]:
        """将字幕分割成块，同时限制每批 JSON 输入规模。"""
        items = list(subtitle_dict.items())
        if not items:
            return []

        max_items = max(1, int(self.batch_num or 1))
        max_chars = max(
            0,
            int(
                getattr(
                    self,
                    "batch_chunk_max_chars",
                    TRANSLATION_CHUNK_MAX_JSON_CHARS,
                )
                or 0
            ),
        )
        chunks: List[Dict[str, str]] = []
        current: List[tuple[str, str]] = []

        for key, value in items:
            candidate = current + [(key, value)]
            candidate_too_large = (
                max_chars > 0
                and current
                and len(json.dumps(dict(candidate), ensure_ascii=False)) > max_chars
            )
            if len(candidate) > max_items or candidate_too_large:
                chunks.append(dict(current))
                current = [(key, value)]
            else:
                current = candidate

        if current:
            chunks.append(dict(current))
        return chunks

    @staticmethod
    def _build_chunk_context(
        chunks: List[Dict[str, str]],
        index: int,
        max_lines: int = 6,
        max_chars: int = 600,
    ) -> str:
        """构建当前批次前文参考，降低后续批次语义漂移"""
        if index <= 0 or max_chars <= 0:
            return ""

        previous_lines = list(chunks[index - 1].values())[-max_lines:]
        context = "\n".join(previous_lines).strip()
        if len(context) > max_chars:
            context = context[-max_chars:]
        return context

    def _parallel_translate(self, chunks: List[Dict[str, str]]) -> Dict[str, str]:
        """并行翻译所有块"""
        if not chunks:
            return {}
        self._init_thread_pool(len(chunks))
        futures = {}
        translated_dict = {}
        pending_results = {}
        next_chunk_index = 0

        for index, chunk in enumerate(chunks):
            context_before = (
                self._build_chunk_context(
                    chunks, index, max_chars=self.batch_context_max_chars
                )
                if self.batch_context_enabled
                else ""
            )
            future = self.executor.submit(
                self._safe_translate_chunk, chunk, context_before
            )
            futures[future] = (index, chunk)

        for future in as_completed(futures):
            if not self.is_running:
                logger.info("翻译器已停止运行，退出翻译")
                break
            index, chunk = futures[future]
            try:
                result = future.result()
                pending_results[index] = result
            except Exception as e:
                logger.error(f"翻译块失败：{str(e)}")
                pending_results[index] = {
                    k: f"{v}||ERROR" for k, v in chunk.items()
                }

            while next_chunk_index in pending_results:
                result = pending_results.pop(next_chunk_index)
                translated_dict.update(result)
                if self.update_callback:
                    self.update_callback(result)
                next_chunk_index += 1

        return translated_dict

    def _safe_translate_chunk(
        self, chunk: Dict[str, str], context_before: str = ""
    ) -> Dict[str, str]:
        """安全的翻译块，包含重试逻辑"""
        for i in range(self.retry_times):
            try:
                return self._translate_chunk(chunk, context_before=context_before)
            except Exception as e:
                if i == self.retry_times - 1:
                    raise
                logger.warning(
                    f"翻译重试 {i+1}/{self.retry_times}: {format_openai_compat_error(e, model_name=getattr(self, 'model', None))}"
                )

    @staticmethod
    def _create_segments(
        original_segments: List[ASRDataSeg], translated_dict: Dict[str, str]
    ) -> List[ASRDataSeg]:
        """创建新的字幕段"""
        for i, seg in enumerate(original_segments, 1):
            try:
                seg.translated_text = translated_dict[str(i)]  # 设置翻译文本
            except Exception as e:
                logger.error(f"创建新的字幕段失败：{str(e)}")
                seg.translated_text = seg.text
        return original_segments

    @abstractmethod
    def _translate_chunk(
        self, subtitle_chunk: Dict[str, str], context_before: str = ""
    ) -> Dict[str, str]:
        """翻译字幕块"""
        pass

    def stop(self):
        """停止翻译器"""
        if not self.is_running:
            return

        logger.info("正在停止翻译器...")
        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"关闭线程池时出错：{str(e)}")
            finally:
                self.executor = None


class OpenAITranslator(BaseTranslator):
    """OpenAI翻译器"""

    def __init__(
        self,
        thread_num: int = 10,
        batch_num: int = 20,
        target_language: str = "Chinese",
        model: str = "gpt-4o-mini",
        custom_prompt: str = "",
        is_reflect: bool = False,
        temperature: float = 0.7,
        timeout: int = 300,
        retry_times: int = 1,
        translation_max_length: int = 0,
        final_translation_rework_max_chars: int = 40,
        use_cache: bool = True,
        batch_context_enabled: bool = True,
        batch_context_max_chars: int = 300,
        update_callback: Optional[Callable] = None,
        usage_callback: Optional[Callable] = None,
        status_callback: Optional[Callable] = None,
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            retry_times=retry_times,
            timeout=timeout,
            use_cache=use_cache,
            batch_context_enabled=batch_context_enabled,
            batch_context_max_chars=batch_context_max_chars,
            update_callback=update_callback,
            usage_callback=usage_callback,
            status_callback=status_callback,
        )

        self._init_client()
        self.model = model
        self.custom_prompt = custom_prompt
        self.is_reflect = is_reflect
        self.temperature = temperature
        self.translation_max_length = max(0, int(translation_max_length or 0))
        self.final_translation_rework_max_chars = max(
            0, int(final_translation_rework_max_chars or 0)
        )

    def _init_client(self):
        """初始化OpenAI客户端"""
        base_url = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY")
        if not (base_url and api_key):
            raise ValueError("环境变量 OPENAI_BASE_URL 和 OPENAI_API_KEY 必须设置")

        self.client = OpenAI(base_url=base_url, api_key=api_key)

    def _get_translate_prompt(self, custom_prompt: Optional[str] = None) -> str:
        """获取翻译提示词"""
        prompt = get_prompt_template(
            PROMPT_REFLECT_TRANSLATE if self.is_reflect else PROMPT_TRANSLATE
        )
        return Template(prompt).safe_substitute(
            target_language=self.target_language,
            custom_prompt=self.custom_prompt if custom_prompt is None else custom_prompt,
            translation_length_instruction=self._get_length_instruction(),
        )

    def _get_length_instruction(self) -> str:
        if self.translation_max_length <= 0:
            return (
                "\n# 字幕翻译优先级:\n"
                "- 完整准确 > 阅读流畅 > 简短凝练。\n"
                "- 不要把翻译改写成摘要；不得省略否定、数字、时间、条件、因果、专有名词、动作对象或术语。\n"
                "- 如果译文较长，优先使用自然、完整的表达，必要时可在译文内部使用换行，而不是删除信息。\n"
            )
        return (
            "\n# 字幕阅读速度建议:\n"
            "- 完整准确 > 阅读流畅 > 简短凝练。\n"
            "- 不要把翻译改写成摘要；不得省略否定、数字、时间、条件、因果、专有名词、动作对象或术语。\n"
            f"- 译文长度建议以 {self.translation_max_length} 作为基础参考，并结合每条字幕时长动态调整。\n"
            "- 长度建议是软约束；如果压缩会丢失信息，可以超过建议长度。\n"
            "- 如果译文较长，优先使用自然、完整的表达，必要时可在译文内部使用换行，而不是删除信息。\n"
        )

    def _is_cjk_target_language(self) -> bool:
        language = str(self.target_language or "").lower()
        cjk_markers = (
            "中文",
            "简体",
            "繁体",
            "漢",
            "汉",
            "日本",
            "日语",
            "韓",
            "韩",
            "粤",
            "粵",
            "chinese",
            "japanese",
            "korean",
            "cantonese",
            "zh",
            "ja",
            "ko",
            "yue",
        )
        return any(marker in language for marker in cjk_markers)

    def _build_reading_budget(self, subtitle_key: str) -> Optional[Dict[str, Any]]:
        timing = getattr(self, "_subtitle_timing_by_key", {}).get(str(subtitle_key))
        if not timing:
            return None

        duration_ms = max(0, int(timing.get("duration_ms", 0)))
        duration_seconds = duration_ms / 1000
        is_cjk = self._is_cjk_target_language()
        budget: Dict[str, Any] = {
            "duration_seconds": duration_seconds,
            "unit": "字" if is_cjk else "词",
            "suggested_length": None,
        }

        if self.translation_max_length <= 0:
            return budget

        if is_cjk:
            dynamic_budget = math.ceil(duration_seconds * CJK_READING_CHARS_PER_SECOND)
            minimum = MIN_CJK_READING_BUDGET
        else:
            dynamic_budget = math.ceil(
                duration_seconds * NON_CJK_READING_WORDS_PER_SECOND
            )
            minimum = MIN_NON_CJK_READING_BUDGET

        budget["suggested_length"] = max(
            minimum,
            int(self.translation_max_length),
            dynamic_budget,
        )
        return budget

    def _build_timing_instruction(self, subtitle_chunk: Dict[str, str]) -> str:
        lines = []
        for key in subtitle_chunk:
            budget = self._build_reading_budget(key)
            if not budget:
                continue
            if budget["suggested_length"] is None:
                lines.append(
                    f"{key}: duration={budget['duration_seconds']:.2f}s, no length target"
                )
            else:
                lines.append(
                    f"{key}: duration={budget['duration_seconds']:.2f}s, "
                    f"suggested_length≈{budget['suggested_length']}{budget['unit']}"
                )

        if not lines:
            return ""

        return (
            "Read-only timing and readability reference for the subtitles below. "
            "Use it only as a soft readability guide. Completeness and accuracy "
            "take priority over these suggestions. Do not output this block.\n"
            "<subtitle_timing>\n"
            + "\n".join(lines)
            + "\n</subtitle_timing>\n\n"
        )

    @staticmethod
    def _count_text_units(text: str) -> int:
        cjk_chars = re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
        words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", text)
        return len(cjk_chars) + len(words)

    @staticmethod
    def _count_final_translation_chars(text: str) -> int:
        return len(re.sub(r"\s+", "", text or ""))

    def _find_overlong_final_translations(
        self, translated_result: Dict[str, str]
    ) -> Dict[str, int]:
        threshold = getattr(self, "final_translation_rework_max_chars", 0)
        if threshold <= 0:
            return {}

        overlong = {}
        for key, text in translated_result.items():
            if not text or text == "ERROR":
                continue
            length = self._count_final_translation_chars(text)
            if length > threshold:
                overlong[key] = length
        return overlong

    def _retranslate_overlong_from_source(
        self,
        subtitle_chunk: Dict[str, str],
        translated_result: Dict[str, str],
        context_before: str,
    ) -> Dict[str, str]:
        overlong = self._find_overlong_final_translations(translated_result)
        if not overlong:
            return {}

        threshold = self.final_translation_rework_max_chars
        logger.info(
            "检测到最终译文超过 %s 字，正在从原文单条重译: %s",
            threshold,
            ", ".join(overlong.keys()),
        )
        retry_chunk = {
            key: subtitle_chunk[key] for key in overlong if key in subtitle_chunk
        }
        if not retry_chunk:
            return {}
        return self._translate_chunk_single(
            retry_chunk,
            context_before=context_before,
            extra_instruction=(
                f"The final subtitle translation must be concise enough to fit within "
                f"{threshold} characters if possible. Re-translate from the original "
                "source text only. Do not refer to, copy, repair, compare against, or "
                "reuse any previous translation. Preserve the full meaning, including "
                "numbers, negation, named entities, terms, actions, objects, conditions, "
                "and causal relations."
            ),
        )

    @staticmethod
    def _extract_numbers(text: str) -> List[str]:
        return re.findall(r"\d+(?:[.,:%:/-]\d+)*%?", text or "")

    @staticmethod
    def _integer_to_chinese(value: int) -> str:
        if value == 0:
            return "零"
        if value < 0 or value > 9999:
            return ""
        digits = "零一二三四五六七八九"
        units = ("", "十", "百", "千")
        result = []
        pending_zero = False
        text = str(value)
        for index, char in enumerate(text):
            digit = int(char)
            position = len(text) - index - 1
            if digit == 0:
                pending_zero = bool(result)
                continue
            if pending_zero:
                result.append("零")
                pending_zero = False
            if not (digit == 1 and position == 1 and not result):
                result.append(digits[digit])
            result.append(units[position])
        return "".join(result)

    @classmethod
    def _number_is_preserved(cls, number: str, translated_text: str) -> bool:
        normalized_number = str(number or "").strip()
        target = str(translated_text or "")
        if normalized_number and normalized_number in target:
            return True
        integer_text = normalized_number.rstrip("%").replace(",", "")
        if not integer_text.isdigit():
            return False
        value = int(integer_text)
        chinese = cls._integer_to_chinese(value)
        english_numbers = {
            0: "zero",
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
            16: "sixteen",
            17: "seventeen",
            18: "eighteen",
            19: "nineteen",
            20: "twenty",
        }
        english = english_numbers.get(value, "")
        return bool(
            (chinese and chinese in target)
            or (english and english in target.casefold())
        )

    @staticmethod
    def _extract_proper_terms(text: str) -> List[str]:
        terms = []
        common_words = {
            "A",
            "An",
            "And",
            "But",
            "For",
            "I",
            "If",
            "It",
            "So",
            "The",
            "This",
            "That",
            "We",
            "You",
        }
        source = text or ""
        first_text_index = len(source) - len(source.lstrip())
        for match in re.finditer(r"\b[A-Z][A-Za-z0-9._+-]{1,}\b", source):
            term = match.group(0)
            if term in common_words:
                continue
            if match.start() == first_text_index and not term.isupper():
                continue
            terms.append(term)
        return sorted(set(terms))

    @staticmethod
    def _has_source_negation(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:not|no|never|cannot|can't|don't|doesn't|didn't|without|won't|isn't|aren't|n't)\b|不|未|无|沒|没|不能|不会|不要",
                text or "",
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _has_target_negation(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:not|no|never|cannot|can't|without|won't|isn't|aren't|n't)\b|不|未|无|沒|没|非|勿|别|不能|不会|不要",
                text or "",
                flags=re.IGNORECASE,
            )
        )

    def _extract_custom_prompt_terms(self) -> List[str]:
        prompt = self.custom_prompt or ""
        terms = re.findall(r"[`'\"“”‘’]([^`'\"“”‘’]{2,40})[`'\"“”‘’]", prompt)
        terms.extend(
            re.findall(
                r"^\s*[-*]?\s*([A-Za-z][A-Za-z0-9._ +/#-]{1,40})\s*(?:=|->|:|：)",
                prompt,
                flags=re.MULTILINE,
            )
        )
        return sorted({term.strip() for term in terms if term.strip()})

    @staticmethod
    def _contains_term(text: str, term: str) -> bool:
        source = str(text or "")
        candidate = str(term or "").strip()
        if not candidate:
            return False
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ +/#'-]*", candidate):
            return bool(
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])",
                    source,
                    flags=re.IGNORECASE,
                )
            )
        return candidate.casefold() in source.casefold()

    def _find_suspicious_compressions(
        self, subtitle_chunk: Dict[str, str], translated_dict: Dict[str, str]
    ) -> Dict[str, List[str]]:
        suspicious = {}
        custom_terms = self._extract_custom_prompt_terms()
        glossary_targets = {
            source.casefold(): target.casefold()
            for source, target in extract_glossary_pairs(self.custom_prompt or "")
        }

        for key, source_text in subtitle_chunk.items():
            translated_text = str(translated_dict.get(key, "") or "")
            reasons = []
            source_units = self._count_text_units(source_text)
            translated_units = self._count_text_units(translated_text)

            if source_text.strip() and not translated_text.strip():
                reasons.append("translated text is empty")
            elif source_units >= 8 and translated_units <= max(
                4, int(source_units * 0.35)
            ):
                reasons.append("translated text is much shorter than the source")

            normalized_translation = translated_text.lower()
            missing_numbers = [
                number
                for number in self._extract_numbers(source_text)
                if number and not self._number_is_preserved(number, translated_text)
            ]
            if missing_numbers:
                reasons.append("numbers may be missing: " + ", ".join(missing_numbers))

            missing_terms = [
                term
                for term in self._extract_proper_terms(source_text)
                if term.lower() not in normalized_translation
            ]
            if missing_terms:
                reasons.append("proper nouns may be missing: " + ", ".join(missing_terms))

            custom_prompt_terms = []
            for term in custom_terms:
                target = glossary_targets.get(term.casefold())
                if (
                    self._contains_term(source_text, term)
                    and not self._contains_term(translated_text, term)
                    and (not target or not self._contains_term(translated_text, target))
                ):
                    custom_prompt_terms.append(term)
            if custom_prompt_terms:
                reasons.append(
                    "custom prompt terms may be missing: "
                    + ", ".join(custom_prompt_terms)
                )

            if self._has_source_negation(source_text) and not self._has_target_negation(
                translated_text
            ):
                reasons.append("source negation may be missing")

            if reasons:
                suspicious[key] = reasons

        return suspicious

    def _postprocess_translated_asr_data(self, asr_data: ASRData) -> ASRData:
        if not self.is_reflect or not asr_data.segments:
            return asr_data

        try:
            self._emit_status("开始反思后处理：检查时间轴、原文与译文")
            items = self._build_reflect_postprocess_items(asr_data)
            overlong_count = sum(1 for item in items if item["is_overlong"])
            self._emit_status(
                f"反思后处理：检测到 {overlong_count} 条可能过长字幕"
            )

            processed_segments = []
            chunks = [
                items[i : i + self.batch_num]
                for i in range(0, len(items), self.batch_num)
            ]
            for index, chunk in enumerate(chunks, 1):
                if not self.is_running:
                    self._emit_status("反思后处理已停止，保留当前反思翻译结果")
                    return asr_data

                self._emit_status(
                    f"反思后处理：AI 润色/拆分第 {index}/{len(chunks)} 批"
                )
                chunk_segments = self._reflect_postprocess_chunk(chunk)
                if chunk_segments is None:
                    chunk_segments = self._items_to_segments(chunk)
                processed_segments.extend(chunk_segments)

            split_delta = len(processed_segments) - len(items)
            self._emit_status(
                f"反思后处理完成：输出 {len(processed_segments)} 条字幕，新增 {max(0, split_delta)} 条"
            )
            return ASRData(processed_segments)
        except Exception as e:
            logger.warning("反思后处理失败，保留原反思翻译结果: %s", str(e))
            self._emit_status(f"反思后处理失败，已回退：{str(e)}")
            return asr_data

    def _build_reflect_postprocess_items(self, asr_data: ASRData) -> List[Dict[str, Any]]:
        items = []
        if not hasattr(self, "_subtitle_timing_by_key"):
            self._subtitle_timing_by_key = {}
        for index, seg in enumerate(asr_data.segments, 1):
            key = str(index)
            timing = {
                "start_time": seg.start_time,
                "end_time": seg.end_time,
                "duration_ms": max(0, seg.end_time - seg.start_time),
            }
            self._subtitle_timing_by_key[key] = timing
            budget = self._build_reading_budget(key) or {}
            suggested_length = budget.get("suggested_length")
            translated_text = seg.translated_text or ""
            translated_units = self._count_text_units(translated_text)
            is_overlong = (
                suggested_length is not None and translated_units > suggested_length
            )
            items.append(
                {
                    "source_key": key,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "duration_ms": timing["duration_ms"],
                    "original_subtitle": seg.text or "",
                    "translated_subtitle": translated_text,
                    "suggested_length": suggested_length,
                    "length_unit": budget.get("unit"),
                    "is_overlong": is_overlong,
                }
            )
        return items

    def _reflect_postprocess_chunk(
        self, chunk: List[Dict[str, Any]]
    ) -> Optional[List[ASRDataSeg]]:
        try:
            cached_segments = self._get_reflect_postprocess_cache(chunk)
            if cached_segments is not None:
                self._emit_status(
                    f"反思后处理：使用缓存结果，{len(chunk)} 条输入 -> {len(cached_segments)} 条输出"
                )
                return cached_segments

            response = self._call_api(
                self._get_reflect_postprocess_prompt(),
                self._get_reflect_postprocess_user_content(chunk),
            )
            if self.usage_callback:
                self.usage_callback("translate", extract_openai_usage(response))

            content = re.sub(
                r"<think>.*?</think>",
                "",
                response.choices[0].message.content,
                flags=re.DOTALL,
            ).strip()
            parsed = json_repair.loads(content)
            segments = self._parse_reflect_postprocess_result(parsed, chunk)
            if segments is None:
                self._emit_status("反思后处理：AI 返回无效，已回退当前批次")
                return None

            self._set_reflect_postprocess_cache(chunk, segments)
            self._emit_status(
                f"反思后处理：当前批次 {len(chunk)} 条输入 -> {len(segments)} 条输出"
            )
            return segments
        except Exception as e:
            logger.warning("反思后处理当前批次失败，已回退: %s", str(e))
            self._emit_status(f"反思后处理：当前批次失败，已回退：{str(e)}")
            return None

    def _get_reflect_postprocess_prompt(self) -> str:
        length_instruction = (
            "没有固定长度上限；以时间轴可读性为准，只在明显过长时拆分。"
            if self.translation_max_length <= 0
            else (
                f"建议参考每条字幕的 suggested_length，并结合 duration_ms 判断是否拆分。"
                f"基础长度参考值为 {self.translation_max_length}。"
            )
        )
        return Template(REFLECT_POSTPROCESS_PROMPT).safe_substitute(
            target_language=self.target_language,
            length_instruction=length_instruction,
        )

    @staticmethod
    def _get_reflect_postprocess_user_content(
        chunk: List[Dict[str, Any]]
    ) -> str:
        return json.dumps(chunk, ensure_ascii=False)

    def _get_reflect_postprocess_cache(
        self, chunk: List[Dict[str, Any]]
    ) -> Optional[List[ASRDataSeg]]:
        if not self.use_cache:
            return None
        cache_result = self.cache_manager.get_llm_result(
            prompt=self._get_reflect_postprocess_user_content(chunk),
            model_name=self.model,
            **self._get_reflect_postprocess_cache_params(),
        )
        if not cache_result:
            return None
        try:
            parsed = json.loads(cache_result)
            return self._parse_reflect_postprocess_result(parsed, chunk)
        except Exception as e:
            logger.warning("反思后处理缓存解析失败，忽略缓存: %s", str(e))
            return None

    def _set_reflect_postprocess_cache(
        self, chunk: List[Dict[str, Any]], segments: List[ASRDataSeg]
    ):
        if not self.use_cache:
            return
        try:
            result = [
                {
                    "source_key": self._find_source_key_for_segment(seg, chunk),
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "original_subtitle": seg.text,
                    "translated_subtitle": seg.translated_text,
                }
                for seg in segments
            ]
            self.cache_manager.set_llm_result(
                prompt=self._get_reflect_postprocess_user_content(chunk),
                result=json.dumps(result, ensure_ascii=False),
                model_name=self.model,
                **self._get_reflect_postprocess_cache_params(),
            )
        except Exception as e:
            logger.warning("反思后处理缓存写入失败，继续使用当前结果: %s", str(e))

    def _get_reflect_postprocess_cache_params(self) -> Dict[str, Any]:
        prompt_hash = hashlib.md5(
            self._get_reflect_postprocess_prompt().encode()
        ).hexdigest()
        return {
            "target_language": self.target_language,
            "temperature": self.temperature,
            "prompt_hash": prompt_hash,
            "translation_max_length": self.translation_max_length,
            "reflect_postprocess_policy_version": REFLECT_POSTPROCESS_POLICY_VERSION,
        }

    @staticmethod
    def _find_source_key_for_segment(seg: ASRDataSeg, chunk: List[Dict[str, Any]]) -> str:
        for item in chunk:
            if item["start_time"] <= seg.start_time and seg.end_time <= item["end_time"]:
                return str(item["source_key"])
        return str(chunk[0]["source_key"]) if chunk else ""

    def _parse_reflect_postprocess_result(
        self, result: Any, chunk: List[Dict[str, Any]]
    ) -> Optional[List[ASRDataSeg]]:
        if isinstance(result, dict) and isinstance(result.get("subtitles"), list):
            result = result["subtitles"]
        if not isinstance(result, list):
            logger.warning("反思后处理返回值不是 JSON 数组")
            return None

        source_items = {str(item["source_key"]): item for item in chunk}
        grouped: Dict[str, List[Dict[str, Any]]] = {
            str(item["source_key"]): [] for item in chunk
        }

        for entry in result:
            if not isinstance(entry, dict):
                logger.warning("反思后处理包含非对象条目")
                return None
            source_key = str(entry.get("source_key", "")).strip()
            if source_key not in source_items:
                logger.warning("反思后处理返回未知 source_key: %s", source_key)
                return None

            try:
                start_time = int(float(entry["start_time"]))
                end_time = int(float(entry["end_time"]))
            except Exception:
                logger.warning("反思后处理返回无效时间轴: %s", entry)
                return None

            source = source_items[source_key]
            original = str(entry.get("original_subtitle", "") or "").strip()
            translated = str(entry.get("translated_subtitle", "") or "").strip()
            if not original or not translated:
                logger.warning("反思后处理返回缺失原文或译文: %s", source_key)
                return None
            if (
                start_time < int(source["start_time"])
                or end_time > int(source["end_time"])
                or start_time >= end_time
            ):
                logger.warning("反思后处理返回越界时间轴: %s", entry)
                return None

            grouped[source_key].append(
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "original_subtitle": original,
                    "translated_subtitle": translated,
                }
            )

        segments = []
        for source_key, source in source_items.items():
            entries = sorted(grouped[source_key], key=lambda item: item["start_time"])
            if not entries:
                logger.warning("反思后处理缺失 source_key: %s", source_key)
                return None
            previous_end = int(source["start_time"])
            for entry in entries:
                if entry["start_time"] < previous_end:
                    logger.warning("反思后处理返回乱序或重叠时间轴: %s", source_key)
                    return None
                previous_end = entry["end_time"]
                segments.append(
                    ASRDataSeg(
                        entry["original_subtitle"],
                        entry["start_time"],
                        entry["end_time"],
                        translated_text=entry["translated_subtitle"],
                    )
                )
        return segments

    @staticmethod
    def _items_to_segments(chunk: List[Dict[str, Any]]) -> List[ASRDataSeg]:
        return [
            ASRDataSeg(
                item["original_subtitle"],
                int(item["start_time"]),
                int(item["end_time"]),
                translated_text=item["translated_subtitle"],
            )
            for item in chunk
        ]

    def _translate_chunk(
        self, subtitle_chunk: Dict[str, str], context_before: str = ""
    ) -> Dict[str, str]:
        """翻译字幕块"""
        logger.info(
            f"[+]正在翻译字幕：{next(iter(subtitle_chunk))} - {next(reversed(subtitle_chunk))}"
        )

        # 获取提示词
        chunk_text = "\n".join(str(text) for text in subtitle_chunk.values())
        filtered_custom_prompt = filter_document_prompt_for_text(
            self.custom_prompt, chunk_text
        )
        prompt = self._get_translate_prompt(filtered_custom_prompt)
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
        timing_instruction = self._build_timing_instruction(subtitle_chunk)
        timing_instruction_hash = hashlib.md5(
            timing_instruction.encode()
        ).hexdigest()

        try:
            # 检查缓存
            cache_params = {
                "target_language": self.target_language,
                "is_reflect": self.is_reflect,
                "temperature": self.temperature,
                "prompt_hash": prompt_hash,
                "context_before": context_before,
                "batch_context_enabled": self.batch_context_enabled,
                "batch_context_max_chars": self.batch_context_max_chars,
                "translation_max_length": self.translation_max_length,
                "final_translation_rework_max_chars": getattr(
                    self, "final_translation_rework_max_chars", 0
                ),
                "translation_readability_policy_version": TRANSLATION_READABILITY_POLICY_VERSION,
                "timing_instruction_hash": timing_instruction_hash,
            }
            cache_key = json.dumps(subtitle_chunk, ensure_ascii=False)
            cache_result = (
                self.cache_manager.get_llm_result(
                    prompt=cache_key,
                    model_name=self.model,
                    **cache_params,
                )
                if self.use_cache
                else None
            )

            result = {}
            if cache_result:
                result = json.loads(cache_result)
            else:
                user_content = json.dumps(subtitle_chunk, ensure_ascii=False)
                if timing_instruction:
                    user_content = timing_instruction + user_content
                if context_before:
                    user_content = (
                        "Reference context from the immediately preceding subtitles. "
                        "Use it only to keep terminology and style consistent. "
                        "Do not translate or output this context.\n"
                        f"<context>{context_before}</context>\n\n"
                        f"{user_content}"
                    )
                # 调用API翻译
                response = self._call_api(prompt, user_content)
                if self.usage_callback:
                    self.usage_callback("translate", extract_openai_usage(response))
                # 解析结果
                result = self._normalize_batch_result(
                    json_repair.loads(response.choices[0].message.content),
                    subtitle_chunk,
                )
                if result is None:
                    logger.warning(f"翻译结果数量不匹配，将使用单条翻译模式重试")
                    return self._translate_chunk_single(subtitle_chunk, context_before)
                # 保存到缓存
                if self.use_cache:
                    self.cache_manager.set_llm_result(
                        prompt=cache_key,
                        result=json.dumps(result, ensure_ascii=False),
                        model_name=self.model,
                        **cache_params,
                    )

            if self.is_reflect:
                result = {k: f"{v['revised_translation']}" for k, v in result.items()}
            else:
                result = {k: f"{v}" for k, v in result.items()}

            suspicious = self._find_suspicious_compressions(subtitle_chunk, result)
            if suspicious:
                logger.info(
                    "检测到疑似过度缩句，正在单条重译: %s",
                    ", ".join(suspicious.keys()),
                )
                retry_chunk = {key: subtitle_chunk[key] for key in suspicious}
                retry_result = self._translate_chunk_single(
                    retry_chunk,
                    context_before=context_before,
                    extra_instruction=(
                        "The previous translation looked like it may have omitted "
                        "important information. Translate fully and faithfully. "
                        "Do not summarize, and keep numbers, negation, named entities, "
                        "conditions, causal relations, actions, objects, and terms."
                    ),
                )
                for key, translated_text in retry_result.items():
                    if translated_text and translated_text != "ERROR":
                        result[key] = translated_text
                if self.use_cache and not self.is_reflect:
                    self.cache_manager.set_llm_result(
                        prompt=cache_key,
                        result=json.dumps(result, ensure_ascii=False),
                        model_name=self.model,
                        **cache_params,
                    )

            retry_result = self._retranslate_overlong_from_source(
                subtitle_chunk, result, context_before
            )
            if retry_result:
                for key, translated_text in retry_result.items():
                    if translated_text and translated_text != "ERROR":
                        result[key] = translated_text
                if self.use_cache and not self.is_reflect:
                    self.cache_manager.set_llm_result(
                        prompt=cache_key,
                        result=json.dumps(result, ensure_ascii=False),
                        model_name=self.model,
                        **cache_params,
                    )

            return result
        except Exception as e:
            try:
                return self._translate_chunk_single(subtitle_chunk, context_before)
            except Exception as e:
                formatted_error = format_openai_compat_error(
                    e, model_name=getattr(self, "model", None)
                )
                logger.error(f"翻译失败：{formatted_error}")
                raise RuntimeError(f"OpenAI API调用失败：{formatted_error}")

    @staticmethod
    def _normalize_batch_result(
        result: Any, subtitle_chunk: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(result, dict) or len(result) != len(subtitle_chunk):
            return None

        expected_keys = list(subtitle_chunk.keys())
        if all(key in result for key in expected_keys):
            return {key: result[key] for key in expected_keys}

        return {
            key: value
            for key, value in zip(expected_keys, result.values())
        }

    def _translate_chunk_single(
        self,
        subtitle_chunk: Dict[str, str],
        context_before: str = "",
        extra_instruction: str = "",
    ) -> Dict[str, str]:
        """单条翻译模式"""
        result = {}
        extra_instruction_hash = hashlib.md5(
            (extra_instruction or "").encode()
        ).hexdigest()
        for idx, text in subtitle_chunk.items():
            try:
                filtered_custom_prompt = filter_document_prompt_for_text(
                    self.custom_prompt, str(text)
                )
                single_prompt = Template(
                    get_prompt_template(PROMPT_SINGLE_TRANSLATE)
                ).safe_substitute(
                    target_language=self.target_language,
                    translation_length_instruction=self._get_length_instruction(),
                )
                if filtered_custom_prompt:
                    single_prompt = (
                        f"{single_prompt.rstrip()}\n\n# 术语或要求:\n"
                        f"{filtered_custom_prompt}"
                    )
                prompt_hash = hashlib.md5(single_prompt.encode()).hexdigest()
                timing_instruction = self._build_timing_instruction({idx: text})
                timing_instruction_hash = hashlib.md5(
                    timing_instruction.encode()
                ).hexdigest()
                # 检查缓存
                cache_params = {
                    "target_language": self.target_language,
                    "is_reflect": self.is_reflect,
                    "temperature": self.temperature,
                    "prompt_hash": prompt_hash,
                    "context_before": context_before,
                    "batch_context_enabled": self.batch_context_enabled,
                    "batch_context_max_chars": self.batch_context_max_chars,
                    "translation_max_length": self.translation_max_length,
                    "final_translation_rework_max_chars": getattr(
                        self, "final_translation_rework_max_chars", 0
                    ),
                    "model": self.model,
                    "translation_readability_policy_version": TRANSLATION_READABILITY_POLICY_VERSION,
                    "timing_instruction_hash": timing_instruction_hash,
                    "compression_retry": bool(extra_instruction),
                    "extra_instruction_hash": extra_instruction_hash,
                }
                cache_result = (
                    self.cache_manager.get_translation(
                        text,
                        "llm",
                        **cache_params,
                    )
                    if self.use_cache
                    else None
                )

                if cache_result:
                    result[idx] = cache_result
                    continue

                user_content = text
                if extra_instruction:
                    user_content = extra_instruction + "\n\n" + user_content
                if timing_instruction:
                    user_content = timing_instruction + user_content
                if context_before:
                    user_content = (
                        "Reference context from the immediately preceding subtitles. "
                        "Use it only to keep terminology and style consistent. "
                        "Do not translate or output this context.\n"
                        f"<context>{context_before}</context>\n\n"
                        f"{user_content}"
                    )
                response = self._call_api(single_prompt, user_content)
                if self.usage_callback:
                    self.usage_callback("translate", extract_openai_usage(response))
                translated_text = (
                    response.choices[0].message.content or ""
                ).strip()

                # 删除 DeepSeek-R1 等推理模型的思考过程 #300
                translated_text = re.sub(
                    r"<think>.*?</think>", "", translated_text, flags=re.DOTALL
                )
                translated_text = translated_text.strip()
                used_source_fallback = False
                if not translated_text:
                    logger.warning("单条翻译返回空译文 %s，使用原文兜底", idx)
                    translated_text = str(text).strip() or "ERROR"
                    used_source_fallback = True

                # 保存到缓存
                if (
                    self.use_cache
                    and translated_text != "ERROR"
                    and not used_source_fallback
                ):
                    self.cache_manager.set_translation(
                        text,
                        translated_text,
                        "llm",
                        **cache_params,
                    )

                result[idx] = translated_text
            except Exception as e:
                logger.error(
                    f"单条翻译失败 {idx}: {format_openai_compat_error(e, model_name=self.model)}"
                )
                result[idx] = "ERROR"  # 如果翻译失败，返回错误标记

        return result

    def _call_api(self, prompt: str, user_content: str) -> Any:
        """调用OpenAI API"""
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]

        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            **get_openai_compat_request_options(
                model_name=self.model, default_timeout=self.timeout
            ),
        )

    def _parse_response(self, response: Any) -> Dict[str, str]:
        """解析API响应"""
        try:
            result = json_repair.loads(response.choices[0].message.content)
            if self.is_reflect:
                return {k: v["revised_translation"] for k, v in result.items()}
            return result
        except Exception as e:
            raise ValueError(f"解析翻译结果失败：{str(e)}")
