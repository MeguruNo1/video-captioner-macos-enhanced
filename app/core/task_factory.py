import datetime
from pathlib import Path
from typing import Optional

from app.common.config import cfg
from app.config import MODEL_PATH, WHISPERX_ONLY_MODE
from app.core.entities import (
    LANGUAGES,
    LLMServiceEnum,
    SplitTypeEnum,
    SubtitleConfig,
    SubtitleTask,
    TranscribeConfig,
    TranscribeModelEnum,
    TranscribeTask,
    TranscriptAndSubtitleTask,
)


class TaskFactory:
    """任务工厂类，用于创建各种类型的任务"""

    @staticmethod
    def create_transcribe_task(
        file_path: str, need_next_task: bool = False
    ) -> TranscribeTask:
        """创建转录任务"""

        # 根据是否需要分段来决定是否需要词级时间戳

        # 获取文件名
        file_name = Path(file_path).stem
        transcribe_model = cfg.transcribe_model.value
        need_word_time_stamp = TaskFactory.get_need_word_time_stamp()

        # 构建输出路径
        if need_next_task:
            need_word_time_stamp = need_word_time_stamp or cfg.need_split.value
            output_path = str(
                Path(cfg.work_dir.value)
                / file_name
                / "subtitle"
                / f"【原始字幕】{file_name}-{transcribe_model.value}-{cfg.transcribe_language.value.value}.srt"
            )
        else:
            output_path = str(Path(file_path).parent / f"{file_name}.srt")

        config = TranscribeConfig(
            transcribe_model=transcribe_model,
            transcribe_language=LANGUAGES[cfg.transcribe_language.value.value],
            use_asr_cache=cfg.use_asr_cache.value,
            need_word_time_stamp=need_word_time_stamp,
            # WhisperX 配置
            whisperx_model=cfg.whisperx_model.value,
            whisperx_device=cfg.whisperx_device.value,
            whisperx_compute_type=cfg.whisperx_compute_type.value,
            whisperx_batch_size=cfg.whisperx_batch_size.value,
            whisperx_auto_language=cfg.whisperx_auto_language.value,
            whisperx_hotwords=cfg.whisperx_hotwords.value,
            whisperx_initial_prompt=cfg.whisperx_initial_prompt.value,
            whisperx_vad_method=cfg.whisperx_vad_method.value,
            whisperx_vad_threshold=cfg.whisperx_vad_threshold.value,
            whisperx_local_silero_dir=cfg.whisperx_local_silero_dir.value,
            whisperx_align=True if WHISPERX_ONLY_MODE else cfg.whisperx_align.value,
            whisperx_model_dir=str(MODEL_PATH),
            # MLX Whisper 配置
            mlx_model=cfg.mlx_model.value,
            mlx_word_timestamps=cfg.mlx_word_timestamps.value,
            mlx_hotwords=cfg.mlx_hotwords.value,
            mlx_initial_prompt=cfg.mlx_initial_prompt.value,
            mlx_vad_enabled=cfg.mlx_vad_enabled.value,
            mlx_vad_threshold=cfg.mlx_vad_threshold.value,
            mlx_chunk_duration=cfg.mlx_chunk_duration.value,
            mlx_chunk_overlap=cfg.mlx_chunk_overlap.value,
        )

        return TranscribeTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            transcribe_config=config,
            need_next_task=need_next_task,
        )

    @staticmethod
    def get_need_word_time_stamp() -> bool:
        if WHISPERX_ONLY_MODE and cfg.transcribe_model.value == TranscribeModelEnum.WHISPER_X:
            return True

        if cfg.transcribe_model.value == TranscribeModelEnum.MLX_WHISPER:
            return cfg.mlx_word_timestamps.value

        return cfg.whisperx_word_timestamps.value

    @staticmethod
    def create_subtitle_task(
        file_path: str, video_path: Optional[str] = None, need_next_task: bool = False
    ) -> SubtitleTask:
        """创建字幕任务"""
        output_name = (
            Path(file_path)
            .stem.replace("【原始字幕】", "")
            .replace(f"【下载字幕】", "")
        )
        suffix = "-LLM 大模型翻译" if cfg.need_translate.value else ""

        output_path = str(Path(file_path).parent / f"【字幕】{output_name}{suffix}.srt")

        if cfg.split_type.value == SplitTypeEnum.SENTENCE.value:
            split_type = "sentence"
        else:
            split_type = "semantic"

        # 根据当前选择的LLM服务获取对应的配置
        current_service = cfg.llm_service.value
        if current_service == LLMServiceEnum.OPENAI:
            base_url = cfg.openai_api_base.value
            api_key = cfg.openai_api_key.value
            llm_model = cfg.openai_model.value
        elif current_service == LLMServiceEnum.SILICON_CLOUD:
            base_url = cfg.silicon_cloud_api_base.value
            api_key = cfg.silicon_cloud_api_key.value
            llm_model = cfg.silicon_cloud_model.value
        elif current_service == LLMServiceEnum.DEEPSEEK:
            base_url = cfg.deepseek_api_base.value
            api_key = cfg.deepseek_api_key.value
            llm_model = cfg.deepseek_model.value
        elif current_service == LLMServiceEnum.OLLAMA:
            base_url = cfg.ollama_api_base.value
            api_key = cfg.ollama_api_key.value
            llm_model = cfg.ollama_model.value
        elif current_service == LLMServiceEnum.LM_STUDIO:
            base_url = cfg.lm_studio_api_base.value
            api_key = cfg.lm_studio_api_key.value
            llm_model = cfg.lm_studio_model.value
        elif current_service == LLMServiceEnum.GEMINI:
            base_url = cfg.gemini_api_base.value
            api_key = cfg.gemini_api_key.value
            llm_model = cfg.gemini_model.value
        elif current_service == LLMServiceEnum.CHATGLM:
            base_url = cfg.chatglm_api_base.value
            api_key = cfg.chatglm_api_key.value
            llm_model = cfg.chatglm_model.value
        elif current_service == LLMServiceEnum.QWEN:
            base_url = cfg.qwen_api_base.value
            api_key = cfg.qwen_api_key.value
            llm_model = cfg.qwen_model.value
        elif current_service == LLMServiceEnum.PUBLIC:
            base_url = cfg.public_api_base.value
            api_key = cfg.public_api_key.value
            llm_model = cfg.public_model.value
        else:
            base_url = ""
            api_key = ""
            llm_model = ""

        config = SubtitleConfig(
            # 翻译配置
            base_url=base_url,
            api_key=api_key,
            llm_model=llm_model,
            llm_service=current_service.value,
            qwen_enable_thinking=cfg.qwen_enable_thinking.value,
            llm_request_timeout=cfg.llm_request_timeout.value,
            llm_cache_enabled=cfg.llm_cache_enabled.value,
            llm_batch_context_enabled=cfg.llm_batch_context_enabled.value,
            llm_batch_context_max_chars=cfg.llm_batch_context_max_chars.value,
            # 字幕处理
            split_type=split_type,
            need_reflect=cfg.need_reflect_translate.value,
            need_translate=cfg.need_translate.value,
            need_optimize=cfg.need_optimize.value,
            thread_num=cfg.thread_num.value,
            batch_size=cfg.batch_size.value,
            translation_max_length=cfg.translation_max_length.value,
            final_translation_rework_max_chars=cfg.final_translation_rework_max_chars.value,
            # 字幕布局
            subtitle_layout=cfg.subtitle_layout.value,
            # 字幕分割
            max_word_count_cjk=cfg.max_word_count_cjk.value,
            max_word_count_english=cfg.max_word_count_english.value,
            need_split=cfg.need_split.value,
            # 字幕翻译
            target_language=cfg.target_language.value.value,
            need_remove_translated_chinese_commas=cfg.needs_remove_translated_chinese_commas.value,
            # 字幕优化
            need_remove_punctuation=cfg.needs_remove_punctuation.value,
            need_mask_original_profanity=cfg.need_mask_original_profanity.value,
            # 字幕提示
            custom_prompt_text=cfg.custom_prompt_text.value,
        )

        return SubtitleTask(
            queued_at=datetime.datetime.now(),
            subtitle_path=file_path,
            video_path=video_path,
            output_path=output_path,
            subtitle_config=config,
            need_next_task=need_next_task,
        )

    @staticmethod
    def create_transcript_and_subtitle_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
    ) -> TranscriptAndSubtitleTask:
        """创建转录和字幕任务"""
        if output_path is None:
            output_path = str(
                Path(file_path).parent / f"{Path(file_path).stem}_processed.srt"
            )

        return TranscriptAndSubtitleTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
        )
