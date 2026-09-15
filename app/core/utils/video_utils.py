from collections.abc import Collection
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Literal, Optional

from ..utils.logger import setup_logger

logger = setup_logger("video_utils")


def _ffmpeg_creationflags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def video2audio(input_file: str, output: str = "") -> bool:
    """使用ffmpeg将视频转换为音频"""
    # 创建output目录
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output = str(output)
    cmd = [
        "ffmpeg",
        "-i",
        input_file,
        "-map",
        "0:a",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-af",
        "aresample=async=1",  # 处理音频同步问题
        "-y",
        output,
    ]
    logger.info("开始提取音频: %s -> %s", input_file, output)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0 and Path(output).is_file():
            return True
        else:
            logger.error("音频转换失败")
            return False
    except Exception as e:
        logger.exception(f"音频转换出错: {str(e)}")
        return False


def check_cuda_available() -> bool:
    """检查CUDA是否可用"""
    logger.info("检查CUDA是否可用")
    try:
        # 首先检查ffmpeg是否支持cuda
        result = subprocess.run(
            ["ffmpeg", "-hwaccels"],
            capture_output=True,
            text=True,
        )
        if "cuda" not in result.stdout.lower():
            logger.info("CUDA不在支持的硬件加速器列表中")
            return False

        # 进一步检查CUDA设备信息
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-init_hw_device", "cuda"],
            capture_output=True,
            text=True,
        )

        # 如果stderr中包含"Cannot load cuda" 或 "Failed to load"等错误信息，说明CUDA不可用
        if any(
            error in result.stderr.lower()
            for error in ["cannot load cuda", "failed to load", "error"]
        ):
            logger.info("CUDA设备初始化失败")
            return False

        logger.info("CUDA可用")
        return True

    except Exception as e:
        logger.exception(f"检查CUDA出错: {str(e)}")
        return False


def get_video_codec(file_path: str) -> str:
    video_info = get_video_info(file_path)
    if not video_info:
        return ""
    return str(video_info.get("video_codec") or "").lower()


def _get_available_ffmpeg_encoders() -> set[str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = f"{result.stdout}\n{result.stderr}"
        encoders = set()
        for line in output.splitlines():
            match = re.match(r"\s*[A-Z\.]+\s+([A-Za-z0-9_]+)\s+", line)
            if match:
                encoders.add(match.group(1))
        return encoders
    except Exception as exc:
        logger.exception("获取 FFmpeg 编码器列表失败: %s", exc)
        return set()


def _get_available_ffmpeg_hwaccels() -> set[str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hwaccels"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_ffmpeg_creationflags(),
        )
        output = f"{result.stdout}\n{result.stderr}"
        hwaccels = set()
        for line in output.splitlines():
            value = line.strip().lower()
            if value and not value.startswith("hardware acceleration"):
                hwaccels.add(value)
        return hwaccels
    except Exception as exc:
        logger.exception("获取 FFmpeg 硬件加速列表失败: %s", exc)
        return set()


def pick_hardware_hevc_encoder() -> Optional[str]:
    encoders = _get_available_ffmpeg_encoders()
    for encoder in ("hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_videotoolbox"):
        if encoder in encoders:
            return encoder
    return None


def pick_software_hevc_encoder() -> Optional[str]:
    encoders = _get_available_ffmpeg_encoders()
    if "libx265" in encoders:
        return "libx265"
    return None


def _build_hevc_transcode_command(
    input_path: Path,
    output_path: Path,
    encoder: str,
    *,
    use_videotoolbox_decode: bool = False,
    transcode_audio_to_aac: bool = False,
) -> list[str]:
    cmd = ["ffmpeg"]
    if use_videotoolbox_decode:
        cmd.extend(["-hwaccel", "videotoolbox"])
    cmd.extend(["-i", str(input_path)])
    if transcode_audio_to_aac:
        cmd.extend(["-map", "0:v:0", "-map", "0:a?", "-c:a", "aac"])
    else:
        cmd.extend(["-map", "0", "-c:a", "copy", "-c:s", "copy"])
    cmd.extend(["-c:v", encoder, "-tag:v", "hvc1"])
    if transcode_audio_to_aac:
        cmd.extend(["-movflags", "+faststart"])
    cmd.extend(["-y", str(output_path)])
    return cmd


def _run_hevc_transcode_command(
    cmd: list[str],
    output_path: Path,
    progress_callback: callable = None,
    progress_message: str = "正在转码为 H.265",
) -> None:
    process = None
    stderr_lines: list[str] = []
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_ffmpeg_creationflags(),
        )

        total_duration = None

        while True:
            output_line = process.stderr.readline()
            if not output_line and process.poll() is not None:
                break
            if not output_line:
                continue

            stderr_lines.append(output_line)
            if progress_callback:
                if total_duration is None:
                    duration_match = re.search(
                        r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", output_line
                    )
                    if duration_match:
                        h, m, s = map(float, duration_match.groups())
                        total_duration = h * 3600 + m * 60 + s

                time_match = re.search(
                    r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})", output_line
                )
                if time_match:
                    h, m, s = map(float, time_match.groups())
                    current_time = h * 3600 + m * 60 + s
                    if total_duration:
                        progress = max(
                            0, min(100, round(current_time / total_duration * 100))
                        )
                        progress_callback(progress, progress_message)

        return_code = process.wait()
        if return_code != 0 or not output_path.is_file():
            error_info = "".join(stderr_lines).strip()
            raise RuntimeError(f"FFmpeg HEVC 转码失败: {error_info or return_code}")
    finally:
        if process and process.poll() is None:
            process.kill()


def transcode_video_to_hevc(
    input_file: str,
    output_file: str,
    progress_callback: callable = None,
    *,
    transcode_audio_to_aac: bool = False,
    encoder_preference: str = "auto",
) -> str:
    input_path = Path(input_file)
    output_path = Path(output_file)

    if not input_path.is_file():
        raise FileNotFoundError(f"输入视频不存在: {input_file}")

    if encoder_preference not in {"auto", "hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"}:
        raise ValueError(f"未知 H.265 编码器: {encoder_preference}")
    if encoder_preference == "auto":
        hardware_encoder = pick_hardware_hevc_encoder()
        software_encoder = pick_software_hevc_encoder()
    else:
        if encoder_preference not in _get_available_ffmpeg_encoders():
            raise RuntimeError(f"当前 FFmpeg 未提供指定编码器 {encoder_preference}；请检查 FFmpeg 或选择自动")
        hardware_encoder = encoder_preference if encoder_preference != "libx265" else None
        software_encoder = "libx265" if encoder_preference == "libx265" else None
    if not hardware_encoder and not software_encoder:
        raise RuntimeError("当前 FFmpeg 环境不可用 HEVC 编码器")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    attempts: list[tuple[str, str, list[str], str]] = []
    if hardware_encoder == "hevc_videotoolbox" and "videotoolbox" in _get_available_ffmpeg_hwaccels():
        attempts.append(
            (
                hardware_encoder,
                "VideoToolbox 硬解 + HEVC 硬编",
                _build_hevc_transcode_command(
                    input_path,
                    output_path,
                    hardware_encoder,
                    use_videotoolbox_decode=True,
                    transcode_audio_to_aac=transcode_audio_to_aac,
                ),
                "正在使用 VideoToolbox 硬解转码为 H.265",
            )
        )
    if hardware_encoder:
        attempts.append(
            (
                hardware_encoder,
                "普通解码 + HEVC 硬编",
                _build_hevc_transcode_command(
                    input_path,
                    output_path,
                    hardware_encoder,
                    transcode_audio_to_aac=transcode_audio_to_aac,
                ),
                "正在转码为 H.265",
            )
        )
    if software_encoder and software_encoder != hardware_encoder:
        attempts.append(
            (
                software_encoder,
                "普通解码 + libx265 软件编码",
                _build_hevc_transcode_command(
                    input_path,
                    output_path,
                    software_encoder,
                    transcode_audio_to_aac=transcode_audio_to_aac,
                ),
                "正在使用 libx265 转码为 H.265",
            )
        )

    last_error: Exception | None = None
    for index, (encoder, label, cmd, progress_message) in enumerate(attempts):
        try:
            logger.info(
                "开始将视频转为 HEVC: %s -> %s (%s, %s)",
                input_file,
                output_file,
                encoder,
                label,
            )
            _run_hevc_transcode_command(
                cmd,
                output_path,
                progress_callback=progress_callback,
                progress_message=progress_message,
            )
            if progress_callback:
                progress_callback(100, "H.265 转码完成")
            logger.info("HEVC 转码完成: %s (%s)", output_path, label)
            if index == 0 and label.startswith("VideoToolbox"):
                return f"{encoder}+videotoolbox_decode"
            return encoder
        except Exception as exc:
            last_error = exc
            if output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    logger.warning("清理失败的 HEVC 输出文件失败: %s", output_path)
            if index < len(attempts) - 1:
                logger.warning(
                    "HEVC 转码尝试失败，回退下一策略: %s: %s", label, exc
                )
                if progress_callback:
                    next_label = attempts[index + 1][1]
                    progress_callback(0, f"{label}失败，回退{next_label}")
                continue
            raise

    raise RuntimeError(f"FFmpeg HEVC 转码失败: {last_error}")


def normalize_video_to_mp4(
    input_file: str,
    output_file: str,
    progress_callback: callable = None,
    force_hevc_for_codecs: Collection[str] | None = None,
    encoder_preference: str = "auto",
) -> str:
    """Normalize a fallback download to an MP4 with Premiere-compatible audio.

    First keep the video stream untouched and encode only audio to AAC. If the
    source video cannot be muxed into MP4, fall back to an HEVC + AAC transcode.
    ``force_hevc_for_codecs`` can force the HEVC path for codecs that may be
    valid in an MP4 container but are still unsuitable for the target workflow.
    """
    input_path = Path(input_file)
    output_path = Path(output_file)
    if not input_path.is_file():
        raise FileNotFoundError(f"输入视频不存在: {input_file}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_name(f".{output_path.stem}.normalizing.mp4")
    if temp_output.exists():
        temp_output.unlink()

    source_codec = get_video_codec(str(input_path))
    force_codecs = {str(codec).lower() for codec in (force_hevc_for_codecs or [])}
    force_hevc = bool(source_codec and source_codec in force_codecs)
    if force_hevc:
        logger.info("当前编码 %s 需要强制转为 H.265: %s", source_codec, input_path)
    else:
        copy_command = [
            "ffmpeg",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-y",
            str(temp_output),
        ]
        logger.info("开始将回退格式规范化为 MP4: %s -> %s", input_path, output_path)
        result = subprocess.run(
            copy_command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_ffmpeg_creationflags(),
        )
        if result.returncode == 0 and temp_output.is_file():
            os.replace(temp_output, output_path)
            if progress_callback:
                progress_callback(100, "MP4 规范化完成")
            logger.info("MP4 规范化完成（视频流复制 + AAC）: %s", output_path)
            return "stream_copy+aac"

        if temp_output.exists():
            temp_output.unlink()
        logger.warning("视频流无法直接封装为 MP4，改为 H.265 + AAC: %s", result.stderr.strip())

    encoder = transcode_video_to_hevc(
        str(input_path),
        str(temp_output),
        progress_callback=progress_callback,
        transcode_audio_to_aac=True,
        encoder_preference=encoder_preference,
    )
    os.replace(temp_output, output_path)
    logger.info("MP4 规范化完成（H.265 + AAC）: %s", output_path)
    return f"{encoder}+aac"


def add_subtitles(
    input_file: str,
    subtitle_file: str,
    output: str,
    quality: Literal[
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    ] = "medium",
    vcodec: str = "libx264",
    soft_subtitle: bool = False,
    progress_callback: callable = None,
) -> None:
    assert Path(input_file).is_file(), "输入文件不存在"
    assert Path(subtitle_file).is_file(), "字幕文件不存在"

    # 移动到临时文件  Fix: 路径错误
    suffix = Path(subtitle_file).suffix.lower()
    if suffix not in {".srt", ".vtt"}:
        raise ValueError(f"Unsupported subtitle format: {suffix}")

    temp_dir = Path(tempfile.gettempdir()) / "VideoCaptioner"
    temp_dir.mkdir(exist_ok=True)
    temp_subtitle = temp_dir / f"temp_subtitle_{uuid.uuid4().hex}.{suffix}"
    shutil.copy2(subtitle_file, temp_subtitle)
    subtitle_file = str(temp_subtitle)

    # 如果是WebM格式，强制使用硬字幕
    if Path(output).suffix.lower() == ".webm":

        soft_subtitle = False
        logger.info("WebM格式视频，强制使用硬字幕")

    if soft_subtitle:
        # 添加软字幕
        cmd = [
            "ffmpeg",
            "-i",
            input_file,
            "-i",
            subtitle_file,
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            output,
            "-y",
        ]
        logger.info(f"添加软字幕执行命令: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:
        logger.info("使用硬字幕")
        subtitle_file = Path(subtitle_file).as_posix().replace(":", r"\:")
        vf = f"subtitles='{subtitle_file}'"

        if Path(output).suffix.lower() == ".webm":
            vcodec = "libvpx-vp9"
            logger.info("WebM格式视频，使用libvpx-vp9编码器")

        # 检查CUDA是否可用
        use_cuda = check_cuda_available()
        cmd = ["ffmpeg"]
        if use_cuda:
            logger.info("使用CUDA加速")
            cmd.extend(["-hwaccel", "cuda"])
        cmd.extend(
            [
                "-i",
                input_file,
                "-acodec",
                "copy",
                "-vcodec",
                vcodec,
                "-preset",
                quality,
                "-vf",
                vf,
                "-y",  # 覆盖输出文件
                output,
            ]
        )

        cmd_str = subprocess.list2cmdline(cmd)
        logger.info(f"添加硬字幕执行命令: {cmd_str}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            # 实时读取输出并调用回调函数
            total_duration = None
            current_time = 0

            while True:
                output_line = process.stderr.readline()
                if not output_line or (process.poll() is not None):
                    break
                if not progress_callback:
                    continue

                if total_duration is None:
                    duration_match = re.search(
                        r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", output_line
                    )
                    if duration_match:
                        h, m, s = map(float, duration_match.groups())
                        total_duration = h * 3600 + m * 60 + s
                        logger.info(f"视频总时长: {total_duration}秒")

                # 解析当前处理时间
                time_match = re.search(
                    r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})", output_line
                )
                if time_match:
                    h, m, s = map(float, time_match.groups())
                    current_time = h * 3600 + m * 60 + s

                # 计算进度百分比
                if total_duration:
                    progress = (current_time / total_duration) * 100
                    progress_callback(f"{round(progress)}", "正在合成")

            if progress_callback:
                progress_callback("100", "合成完成")
            # 检查进程的返回码
            return_code = process.wait()
            if return_code != 0:
                error_info = process.stderr.read()
                logger.error(f"视频合成失败， {error_info}")
                raise Exception(return_code)
            logger.info("视频合成完成")

        except Exception as e:
            logger.exception(f"关闭 FFmpeg: {str(e)}")
            if process and process.poll() is None:  # 如果进程还在运行
                process.kill()  # 如果进程没有及时终止，强制结束它
            raise
        finally:
            # 删除临时文件
            if temp_subtitle.exists():
                temp_subtitle.unlink()


def get_video_info(file_path: str) -> Optional[Dict]:
    """获取视频信息"""
    try:
        cmd = ["ffmpeg", "-i", file_path]

        # logger.info(f"获取视频信息执行命令: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        info = result.stderr

        video_info_dict = {
            "file_name": Path(file_path).stem,
            "file_path": file_path,
            "duration_seconds": 0,
            "bitrate_kbps": 0,
            "video_codec": "",
            "width": 0,
            "height": 0,
            "fps": 0,
            "audio_codec": "",
            "audio_sampling_rate": 0,
            "thumbnail_path": "",
        }

        # 提取时长
        if duration_match := re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info):
            hours, minutes, seconds = map(float, duration_match.groups())
            video_info_dict["duration_seconds"] = hours * 3600 + minutes * 60 + seconds

        # 提取比特率
        if bitrate_match := re.search(r"bitrate: (\d+) kb/s", info):
            video_info_dict["bitrate_kbps"] = int(bitrate_match.group(1))

        # 提取视频流信息
        if video_stream_match := re.search(
            r"Stream #.*?Video: (\w+)(?:\s*\([^)]*\))?.* (\d+)x(\d+).*?(?:(\d+(?:\.\d+)?)\s*(?:fps|tb[rn]))",
            info,
            re.DOTALL,
        ):
            video_info_dict.update(
                {
                    "video_codec": video_stream_match.group(1),
                    "width": int(video_stream_match.group(2)),
                    "height": int(video_stream_match.group(3)),
                    "fps": float(video_stream_match.group(4)),
                }
            )
        else:
            logger.warning("未找到视频流信息")

        return video_info_dict
    except Exception as e:
        logger.exception(f"获取视频信息时出错: {str(e)}")
        return None
