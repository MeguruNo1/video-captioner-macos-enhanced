import datetime
from copy import deepcopy
import os
from pathlib import Path
from typing import Dict
from uuid import uuid4

from PyQt5.QtCore import QSettings, QThread, pyqtSignal

from app.common.config import cfg
from app.core.bk_asr.asr_data import ASRData
from app.core.entities import (
    SubtitleConfig,
    SubtitleTask,
    TargetLanguageEnum,
)
from app.core.utils.logger import setup_logger

# 配置日志
logger = setup_logger("subtitle_optimization_thread")

CHINESE_TARGET_LANGUAGES = {
    TargetLanguageEnum.CHINESE.value,
}


class SubtitleThread(QThread):
    finished = pyqtSignal(str, str)
    progress = pyqtSignal(int, str)
    token_progress = pyqtSignal(str)
    update = pyqtSignal(dict)
    update_all = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, task: SubtitleTask):
        super().__init__()
        self.task: SubtitleTask = task
        self.input_data = None
        self.subtitle_length = 0
        self.finished_subtitle_length = 0
        self.custom_prompt_text = ""
        self.last_progress_value = 0
        self.last_progress_status = ""
        self.token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def set_input_data(self, data):
        """Freeze edited rows without overwriting the imported subtitle file."""
        self.input_data = deepcopy(data) if data is not None else None

    def _load_input_subtitles(self):
        if self.input_data is not None:
            return ASRData.from_json(self.input_data)
        source = Path(self.task.subtitle_path)
        sidecar = source.with_name(f"{source.stem}.asr.json")
        return ASRData.from_subtitle_file(str(sidecar if sidecar.exists() else source))

    def set_custom_prompt_text(self, text: str):
        self.custom_prompt_text = text

    def _setup_api_config(self) -> SubtitleConfig:
        """设置API配置，返回SubtitleConfig"""
        if self.task.subtitle_config.base_url and self.task.subtitle_config.api_key:
            from app.core.utils.test_opanai import test_openai

            if not test_openai(
                self.task.subtitle_config.base_url,
                self.task.subtitle_config.api_key,
                self.task.subtitle_config.llm_model,
                self.task.subtitle_config.llm_service,
                self.task.subtitle_config.qwen_enable_thinking,
                self.task.subtitle_config.llm_request_timeout,
            )[0]:
                raise Exception(
                    self.tr(
                        "（字幕断句或字幕修正需要大模型）\nLLM API 测试失败, 请检查LLM配置"
                    )
                )
            return self.task.subtitle_config
        else:
            raise Exception(
                self.tr(
                    "（字幕断句或字幕修正需要大模型）\nLLM API 未配置, 请检查LLM配置"
                )
            )

    def run(self):
        try:
            logger.info(f"\n===========字幕处理任务开始===========")
            logger.info(f"时间：{datetime.datetime.now()}")

            # 字幕文件路径检查、对断句字幕路径进行定义
            subtitle_path = self.task.subtitle_path
            subtitle_path_obj = Path(subtitle_path)
            output_name = (
                subtitle_path_obj
                .stem.replace("【原始字幕】", "")
                .replace("【下载字幕】", "")
            )
            split_path = str(
                Path(subtitle_path).parent / f"【断句字幕】{output_name}.srt"
            )
            assert subtitle_path is not None, self.tr("字幕文件路径为空")

            subtitle_config = self.task.subtitle_config

            asr_data = self._load_input_subtitles()

            # 1. 分割成字词级时间戳（对于非断句字幕且开启分割选项）
            if subtitle_config.need_split and not asr_data.is_word_timestamp():
                asr_data.split_to_word_segments()

            # 验证当前任务配置的 LLM 服务
            if (
                subtitle_config.need_optimize
                or asr_data.is_word_timestamp()
                or subtitle_config.need_translate
            ):
                self._emit_progress(2, self.tr("开始验证API配置..."))
                subtitle_config = self._setup_api_config()
                os.environ["OPENAI_BASE_URL"] = subtitle_config.base_url
                os.environ["OPENAI_API_KEY"] = subtitle_config.api_key
                os.environ["OPENAI_COMPAT_SERVICE"] = subtitle_config.llm_service or ""
                os.environ["OPENAI_QWEN_ENABLE_THINKING"] = (
                    "true" if subtitle_config.qwen_enable_thinking else "false"
                )
                logger.info(
                    "LLM API timeout set to %s seconds",
                    subtitle_config.llm_request_timeout,
                )

            # 2. 重新断句（对于字词级字幕）
            if asr_data.is_word_timestamp():
                from app.core.subtitle_processor.split import SubtitleSplitter

                self._emit_progress(5, self.tr("字幕断句..."))
                logger.info("正在字幕断句...")
                splitter = SubtitleSplitter(
                    thread_num=subtitle_config.thread_num,
                    model=subtitle_config.llm_model,
                    temperature=0.3,
                    timeout=subtitle_config.llm_request_timeout,
                    retry_times=1,
                    split_type=subtitle_config.split_type,
                    max_word_count_cjk=subtitle_config.max_word_count_cjk,
                    max_word_count_english=subtitle_config.max_word_count_english,
                    use_cache=subtitle_config.llm_cache_enabled,
                    usage_callback=self.usage_callback,
                )
                asr_data = splitter.split_subtitle(asr_data)
                asr_data.save(save_path=split_path)
                self.update_all.emit(asr_data.to_json())

            # 3. 优化字幕
            custom_prompt = subtitle_config.custom_prompt_text
            self.subtitle_length = len(asr_data.segments)

            if subtitle_config.need_optimize:
                from app.core.subtitle_processor.optimize import SubtitleOptimizer

                self._emit_progress(0, self.tr("优化字幕..."))
                logger.info("正在优化字幕...")
                self.finished_subtitle_length = 0  # 重置计数器
                optimizer = SubtitleOptimizer(
                    custom_prompt=custom_prompt,
                    model=subtitle_config.llm_model,
                    batch_num=subtitle_config.batch_size,
                    thread_num=subtitle_config.thread_num,
                    update_callback=self.callback,
                    usage_callback=self.usage_callback,
                    timeout=subtitle_config.llm_request_timeout,
                    use_cache=subtitle_config.llm_cache_enabled,
                    batch_context_enabled=subtitle_config.llm_batch_context_enabled,
                    batch_context_max_chars=subtitle_config.llm_batch_context_max_chars,
                )
                self.optimizer = optimizer
                asr_data = optimizer.optimize_subtitle(asr_data)
                self.update_all.emit(asr_data.to_json())

            # 4. 翻译字幕
            if subtitle_config.need_translate:
                from app.core.subtitle_processor.translate import OpenAITranslator

                self._emit_progress(0, self.tr("翻译字幕..."))
                logger.info("正在翻译字幕...")
                self.finished_subtitle_length = 0  # 重置计数器
                translator = OpenAITranslator(
                    thread_num=subtitle_config.thread_num,
                    batch_num=subtitle_config.batch_size,
                    target_language=subtitle_config.target_language,
                    model=subtitle_config.llm_model,
                    custom_prompt=custom_prompt,
                    is_reflect=subtitle_config.need_reflect,
                    update_callback=self.callback,
                    usage_callback=self.usage_callback,
                    timeout=subtitle_config.llm_request_timeout,
                    translation_max_length=subtitle_config.translation_max_length,
                    final_translation_rework_max_chars=subtitle_config.final_translation_rework_max_chars,
                    use_cache=subtitle_config.llm_cache_enabled,
                    batch_context_enabled=subtitle_config.llm_batch_context_enabled,
                    batch_context_max_chars=subtitle_config.llm_batch_context_max_chars,
                    status_callback=self.status_callback,
                )
                self.translator = translator
                asr_data = translator.translate_subtitle(asr_data)
                if (
                    subtitle_config.need_remove_translated_chinese_commas
                    and subtitle_config.target_language in CHINESE_TARGET_LANGUAGES
                ):
                    asr_data.remove_translated_chinese_commas()
                # 仅删除译文中的全角句号
                if subtitle_config.need_remove_punctuation:
                    asr_data.remove_translated_periods()
                self.update_all.emit(asr_data.to_json())

            # 5. 屏蔽原文字幕中的脏话，只修改原文，不影响译文
            if subtitle_config.need_mask_original_profanity:
                self._emit_progress(95, self.tr("屏蔽原文脏话..."))
                asr_data.mask_original_profanity()
                self.update_all.emit(asr_data.to_json())

            if subtitle_config.target_language in CHINESE_TARGET_LANGUAGES:
                asr_data.normalize_translated_cjk_quotes()

            # 6. 保存字幕
            # 对所有入口统一补齐短显示空隙，包括无需重新断句的已有字幕。
            from app.core.subtitle_processor.split import SubtitleSplitter

            SubtitleSplitter._fill_short_display_gaps(asr_data.segments)
            self._atomic_save_subtitles(asr_data, subtitle_config.subtitle_layout)
            logger.info(f"字幕保存到 {self.task.output_path}")

            # 6. 清理中间断句文件
            split_path = str(
                Path(self.task.subtitle_path).parent
                / f"【智能断句】{Path(self.task.subtitle_path).stem}.srt"
            )
            if os.path.exists(split_path):
                os.remove(split_path)

            self._emit_progress(100, self.tr("优化完成"))
            logger.info("优化完成")
            self.finished.emit(self.task.video_path, self.task.output_path)
        except Exception as e:
            logger.exception(f"优化失败: {str(e)}")
            self.error.emit(str(e))
            self._emit_progress(100, self.tr("优化失败"))

    def _atomic_save_subtitles(self, asr_data: ASRData, layout: str) -> None:
        output_path = Path(self.task.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        temp_path = output_path.with_name(
            f".{output_path.stem}.{token}.partial{output_path.suffix}"
        )
        replacements = [(temp_path, output_path)]
        if layout == "单独输出原文和译文":
            for suffix in ("-仅原文", "-仅译文"):
                replacements.append(
                    (
                        temp_path.with_name(
                            f"{temp_path.stem}{suffix}{temp_path.suffix}"
                        ),
                        output_path.with_name(
                            f"{output_path.stem}{suffix}{output_path.suffix}"
                        ),
                    )
                )

        try:
            asr_data.save(save_path=str(temp_path), layout=layout)
            missing = [str(path) for path, _ in replacements if not path.exists()]
            if missing:
                raise RuntimeError("字幕临时文件生成不完整: " + ", ".join(missing))
            for temporary, target in replacements:
                os.replace(temporary, target)
        finally:
            for temporary, _ in replacements:
                temporary.unlink(missing_ok=True)

    def callback(self, result: Dict):
        self.finished_subtitle_length += len(result)
        # 简单计算当前进度（0-100%）
        progress = min(
            int((self.finished_subtitle_length / self.subtitle_length) * 100), 100
        )
        self._emit_progress(progress, self.tr("{0}% 处理字幕").format(progress))
        self.update.emit(result)

    def usage_callback(self, stage: str, usage: Dict):
        self.token_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        self.token_usage["completion_tokens"] += int(
            usage.get("completion_tokens", 0) or 0
        )
        self.token_usage["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        if self.last_progress_status:
            self.token_progress.emit(
                f"{self.last_progress_status}{self._format_usage_suffix()}"
            )

    def status_callback(self, message: str):
        logger.info(message)
        self._emit_progress(self.last_progress_value, self.tr(message))

    def _format_usage_suffix(self) -> str:
        total = self.token_usage["total_tokens"]
        if total <= 0:
            return ""
        return self.tr(" | Tokens: {0}（输入 {1} / 输出 {2}）").format(
            total,
            self.token_usage["prompt_tokens"],
            self.token_usage["completion_tokens"],
        )

    def _emit_progress(self, value: int, status: str):
        self.last_progress_value = value
        self.last_progress_status = status
        self.progress.emit(value, status)

    def stop(self):
        """停止所有处理"""
        try:
            # 先停止优化器
            if hasattr(self, "optimizer"):
                try:
                    self.optimizer.stop()
                except Exception as e:
                    logger.error(f"停止优化器时出错：{str(e)}")

            if hasattr(self, "translator"):
                try:
                    self.translator.stop()
                except Exception as e:
                    logger.error(f"停止翻译器时出错：{str(e)}")

            # 终止线程
            self.terminate()
            # 等待最多3秒
            if not self.wait(3000):
                logger.warning("线程未能在3秒内正常停止")

            # 发送进度信号
            self._emit_progress(100, self.tr("已终止"))

        except Exception as e:
            logger.error(f"停止线程时出错：{str(e)}")
            self._emit_progress(100, self.tr("终止时发生错误"))
