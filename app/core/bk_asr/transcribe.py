from app.core.bk_asr.asr_data import ASRData
from app.core.entities import TranscribeConfig, TranscribeModelEnum


def _remove_repeated_artifacts_if_word_timestamp(asr_data):
    if isinstance(asr_data, ASRData) and asr_data.is_word_timestamp():
        asr_data.remove_repeated_asr_artifacts()
    return asr_data


def transcribe(audio_path: str, config: TranscribeConfig, callback=None) -> ASRData:
    """
    使用指定的转录配置对音频文件进行转录

    Args:
        audio_path: 音频文件路径
        config: 转录配置
        callback: 进度回调函数,接收两个参数(progress: int, message: str)

    Returns:
        ASRData: 转录结果数据
    """
    if callback is None:
        callback = lambda x, y: None

    if config.transcribe_model == TranscribeModelEnum.MLX_WHISPER:
        from app.core.bk_asr.mlx_whisper import (
            MLXWhisperASR,
            build_mlx_initial_prompt,
        )

        asr = MLXWhisperASR(
            audio_path,
            use_cache=config.use_asr_cache,
            need_word_time_stamp=config.mlx_word_timestamps,
            model=config.mlx_model,
            language=config.transcribe_language,
            initial_prompt=build_mlx_initial_prompt(
                config.mlx_initial_prompt,
                config.mlx_hotwords,
            ),
            vad_enabled=config.mlx_vad_enabled,
            vad_threshold=config.mlx_vad_threshold,
            chunk_duration=config.mlx_chunk_duration,
            chunk_overlap=config.mlx_chunk_overlap,
            align_device=config.whisperx_device,
            align_model_dir=config.whisperx_model_dir,
        )
        asr_data = asr.run(callback=callback)
        _remove_repeated_artifacts_if_word_timestamp(asr_data)
        if not config.mlx_word_timestamps:
            asr_data.optimize_timing()
        return asr_data

    config.transcribe_model = TranscribeModelEnum.WHISPER_X
    config.need_word_time_stamp = True
    config.whisperx_align = True

    # 构建ASR参数
    asr_args = {
        "use_cache": config.use_asr_cache,
        "need_word_time_stamp": config.need_word_time_stamp,
    }

    asr_args.update(
        {
            "language": (
                None if config.whisperx_auto_language else config.transcribe_language
            ),
            "whisper_model": config.whisperx_model,
            "device": config.whisperx_device,
            "compute_type": config.whisperx_compute_type,
            "batch_size": config.whisperx_batch_size,
            "hotwords": config.whisperx_hotwords,
            "initial_prompt": config.whisperx_initial_prompt,
            "vad_method": config.whisperx_vad_method,
            "vad_threshold": config.whisperx_vad_threshold,
            "local_silero_dir": config.whisperx_local_silero_dir,
            "align": config.whisperx_align,
            "model_dir": config.whisperx_model_dir,
        }
    )

    # 创建ASR实例并运行
    from app.core.bk_asr.whisper_x_auto import WhisperXASR

    asr = WhisperXASR(audio_path, **asr_args)

    asr_data = asr.run(callback=callback)
    _remove_repeated_artifacts_if_word_timestamp(asr_data)

    # 优化字幕显示时间 #161
    if not config.need_word_time_stamp:
        asr_data.optimize_timing()

    return asr_data
