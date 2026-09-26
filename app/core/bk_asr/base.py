import json
import os
import tempfile
import threading
import zlib
from typing import Optional, Union

from app.config import CACHE_PATH
from app.core.storage.cache_manager import CacheManager

from .asr_data import ASRData, ASRDataSeg


class BaseASR:
    SUPPORTED_SOUND_FORMAT = ["flac", "m4a", "mp3", "wav"]
    _lock = threading.Lock()

    def __init__(
        self,
        audio_path: Optional[Union[str, bytes]] = None,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
    ):
        self.audio_path = audio_path
        self.file_binary = None
        self.use_cache = use_cache
        self.need_word_time_stamp = need_word_time_stamp
        self._set_data()
        self.cache_manager = CacheManager(str(CACHE_PATH))

    def _set_data(self):
        if isinstance(self.audio_path, bytes):
            self.file_binary = self.audio_path
        else:
            ext = self.audio_path.split(".")[-1].lower()
            assert (
                ext in self.SUPPORTED_SOUND_FORMAT
            ), f"Unsupported sound format: {ext}"
            assert os.path.exists(self.audio_path), f"File not found: {self.audio_path}"
            with open(self.audio_path, "rb") as f:
                self.file_binary = f.read()
        crc32_value = zlib.crc32(self.file_binary) & 0xFFFFFFFF
        self.crc32_hex = format(crc32_value, "08x")

    def run(self, callback=None, **kwargs) -> ASRData:
        if self.use_cache:
            cached_result = self.cache_manager.get_asr_result(
                self._get_key(), self.__class__.__name__
            )
            if cached_result:
                segments = self._make_segments(cached_result)

                return ASRData(segments)

        resp_data = self._run(callback, **kwargs)

        if self.use_cache:
            self.cache_manager.set_asr_result(
                self._get_key(), self.__class__.__name__, resp_data
            )

        segments = self._make_segments(resp_data)
        return ASRData(segments)

    def _get_key(self):
        """获取缓存key"""
        return self.crc32_hex

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        """Convert the shared Whisper result schema without changing timing rules."""
        segments = []

        if self.need_word_time_stamp:
            for segment in resp_data.get("segments", []):
                for word in segment.get("words", []) or []:
                    text = (word.get("word") or word.get("text") or "").strip()
                    start = word.get("start")
                    end = word.get("end")
                    if not text or start is None or end is None:
                        continue
                    segments.append(
                        ASRDataSeg(
                            text=text,
                            start_time=int(float(start) * 1000),
                            end_time=int(float(end) * 1000),
                            speaker=word.get("speaker") or segment.get("speaker") or "",
                        )
                    )

        if segments:
            return segments

        for segment in resp_data.get("segments", []):
            text = (segment.get("text") or "").strip()
            start = segment.get("start")
            end = segment.get("end")
            if not text or start is None or end is None:
                continue
            segments.append(
                ASRDataSeg(
                    text=text,
                    start_time=int(float(start) * 1000),
                    end_time=int(float(end) * 1000),
                    speaker=segment.get("speaker") or "",
                )
            )

        return segments

    def _run(self, callback=None, **kwargs) -> dict:
        """运行ASR服务并返回响应数据"""
        raise NotImplementedError("_run method must be implemented in subclass")
