import html
import json
import math
import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from app.core.utils.profanity_filter import mask_english_profanity
from app.core.utils.subtitle_punctuation import normalize_cjk_quotes

SEPARATE_ORIGINAL_TRANSLATE_LAYOUT = "单独输出原文和译文"


class ASRDataSeg:
    def __init__(
        self,
        text: str,
        start_time: int,
        end_time: int,
        translated_text: str = "",
        speaker: str = "",
    ):
        self.text = text
        self.translated_text = translated_text
        self.speaker = speaker
        self.start_time = start_time
        self.end_time = end_time

    def to_srt_ts(self) -> str:
        """Convert to SRT timestamp format"""
        return f"{self._ms_to_srt_time(self.start_time)} --> {self._ms_to_srt_time(self.end_time)}"

    def to_lrc_ts(self) -> str:
        """Convert to LRC timestamp format"""
        return f"[{self._ms_to_lrc_time(self.start_time)}]"

    def _ms_to_lrc_time(self, ms: int) -> str:
        seconds = ms / 1000
        minutes, seconds = divmod(seconds, 60)
        return f"{int(minutes):02}:{seconds:.2f}"

    @staticmethod
    def _ms_to_srt_time(ms: int) -> str:
        """Convert milliseconds to SRT time format (HH:MM:SS,mmm)"""
        total_seconds, milliseconds = divmod(ms, 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02},{int(milliseconds):03}"

    @property
    def transcript(self) -> str:
        """Return segment text"""
        return self.text

    def __str__(self) -> str:
        return f"ASRDataSeg({self.text}, {self.start_time}, {self.end_time})"


def merge_speakers(segments: Iterable[ASRDataSeg]) -> str:
    speakers = {
        seg.speaker.strip() for seg in segments if seg.speaker and seg.speaker.strip()
    }
    if len(speakers) == 1:
        return speakers.pop()
    return ""


class ASRData:
    def __init__(self, segments: List[ASRDataSeg]):
        # 去除 segments.text 为空的
        filtered_segments = [seg for seg in segments if seg.text and seg.text.strip()]
        filtered_segments.sort(key=lambda x: x.start_time)
        self.segments = filtered_segments

    def __iter__(self):
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def has_data(self) -> bool:
        """Check if there are any utterances"""
        return len(self.segments) > 0

    def is_word_timestamp(self) -> bool:
        """
        判断是否是字级时间戳
        规则：
        1. 对于英文，每个segment应该只包含一个单词
        2. 对于中文，每个segment应该只包含一个汉字
        3. 允许20%的误差率
        """
        if not self.segments:
            return False

        valid_segments = 0
        total_segments = len(self.segments)

        for seg in self.segments:
            text = seg.text.strip()
            # 检查是否只包含一个英文单词或一个汉字
            if (len(text.split()) == 1 and text.isascii()) or len(text.strip()) <= 2:
                valid_segments += 1
        return (valid_segments / total_segments) >= 0.8

    @staticmethod
    def _duplicate_artifact_key(text: str) -> str:
        return re.sub(r"^[\W_]+|[\W_]+$", "", (text or "").strip().casefold())

    @classmethod
    def _phrase_artifact_key(cls, text: str) -> str:
        """Normalize words and retain punctuation-only phrase separators."""
        lexical_key = cls._duplicate_artifact_key(text)
        if lexical_key:
            return lexical_key

        punctuation = re.sub(r"\s+", "", (text or "").strip().casefold())
        if punctuation:
            return f"__punctuation__:{punctuation}"
        return ""

    def remove_long_duplicate_runs(self, min_run_length: int = 10) -> "ASRData":
        """Collapse long ASR hallucination runs like word word word ..."""
        min_run_length = max(2, int(min_run_length or 2))
        if not self.segments:
            return self

        cleaned: List[ASRDataSeg] = []
        current_run: List[ASRDataSeg] = []
        current_key = ""

        def flush_run():
            if not current_run:
                return
            if current_key and len(current_run) >= min_run_length:
                cleaned.append(current_run[0])
            else:
                cleaned.extend(current_run)

        for seg in self.segments:
            key = self._duplicate_artifact_key(seg.text)
            if key and key == current_key:
                current_run.append(seg)
                continue

            flush_run()
            current_run = [seg]
            current_key = key

        flush_run()
        self.segments = cleaned
        return self

    def remove_repeated_phrase_runs(
        self,
        min_repetitions: int = 4,
        max_phrase_words: int = 5,
    ) -> "ASRData":
        """Collapse repeated short phrase loops emitted by word-level ASR."""
        min_repetitions = max(2, int(min_repetitions or 2))
        max_phrase_words = max(2, int(max_phrase_words or 2))
        if len(self.segments) < min_repetitions * 2:
            return self

        # Punctuation-only word-timestamp segments must remain part of the
        # pattern. Otherwise loops such as ``5 - 5 - 5 - ...`` become
        # ``["5", "", "5", "", ...]`` and bypass duplicate detection.
        keys = [self._phrase_artifact_key(seg.text) for seg in self.segments]
        keep = [True] * len(self.segments)
        index = 0
        while index < len(keys):
            found_loop = False
            # Prefer the shortest repeating unit. Checking longer phrases first
            # leaves duplicate cycles behind for patterns such as A-B-A-B.
            for phrase_len in range(2, max_phrase_words + 1):
                end = index + phrase_len * min_repetitions
                if end > len(keys):
                    continue
                phrase = keys[index : index + phrase_len]
                if not all(phrase):
                    continue
                repeats = 1
                while (
                    index + (repeats + 1) * phrase_len <= len(keys)
                    and keys[index : index + phrase_len]
                    == keys[
                        index + repeats * phrase_len : index
                        + (repeats + 1) * phrase_len
                    ]
                ):
                    repeats += 1
                if repeats < min_repetitions:
                    continue

                for remove_index in range(
                    index + phrase_len,
                    index + repeats * phrase_len,
                ):
                    keep[remove_index] = False

                # ASR loops can stop midway through the next cycle (for
                # example ``5 -`` repeated many times and ending on ``5``).
                # Drop that matching suffix as part of the same artifact.
                run_end = index + repeats * phrase_len
                partial_len = 0
                while (
                    partial_len < phrase_len
                    and run_end + partial_len < len(keys)
                    and keys[run_end + partial_len] == phrase[partial_len]
                ):
                    keep[run_end + partial_len] = False
                    partial_len += 1

                index = run_end + partial_len
                found_loop = True
                break
            if not found_loop:
                index += 1

        self.segments = [
            seg for seg, should_keep in zip(self.segments, keep) if should_keep
        ]
        return self

    def remove_repeated_asr_artifacts(self) -> "ASRData":
        self.remove_long_duplicate_runs()
        self.remove_repeated_phrase_runs()
        return self

    def split_to_word_segments(self) -> "ASRData":
        """
        将当前ASRData中的每个segment按字词分割，并按音素计算时间戳
        每4个字符视为一个音素单位进行时间分配

        Returns:
            ASRData: 包含分割后字词级别segments的新ASRData实例
        """
        CHARS_PER_PHONEME = 4  # 每个音素包含的字符数
        new_segments = []

        for seg in self.segments:
            text = seg.text
            duration = seg.end_time - seg.start_time

            # 匹配所有有效字符（包括数字和各种语言）
            pattern = (
                # 以单词形式出现的语言(连续提取)
                r"[a-zA-Z\u00c0-\u00ff\u0100-\u017f']+"  # 拉丁字母及其变体(英语、德语、法语等)
                r"|[\u0400-\u04ff]+"  # 西里尔字母(俄语等)
                r"|[\u0370-\u03ff]+"  # 希腊语
                r"|[\u0600-\u06ff]+"  # 阿拉伯语
                r"|[\u0590-\u05ff]+"  # 希伯来语
                r"|\d+"  # 数字
                # 以单字形式出现的语言(单字提取)
                r"|[\u4e00-\u9fff]"  # 中文
                r"|[\u3040-\u309f]"  # 日文平假名
                r"|[\u30a0-\u30ff]"  # 日文片假名
                r"|[\uac00-\ud7af]"  # 韩文
                r"|[\u0e00-\u0e7f][\u0e30-\u0e3a\u0e47-\u0e4e]*"  # 泰文基字符及其音标组合
                r"|[\u0900-\u097f]"  # 天城文(印地语等)
                r"|[\u0980-\u09ff]"  # 孟加拉语
                r"|[\u0e80-\u0eff]"  # 老挝文
                r"|[\u1000-\u109f]"  # 缅甸文
            )
            words = re.finditer(pattern, text)
            words_list = list(words)

            if not words_list:
                continue

            # 计算总音素数
            total_phonemes = sum(
                math.ceil(len(w.group()) / CHARS_PER_PHONEME) for w in words_list
            )
            time_per_phoneme = duration / max(total_phonemes, 1)  # 防止除零

            current_time = seg.start_time
            for word_match in words_list:
                word = word_match.group()
                # 计算当前词的音素数
                word_phonemes = math.ceil(len(word) / CHARS_PER_PHONEME)
                word_duration = int(time_per_phoneme * word_phonemes)

                # 创建新的字词级segment
                word_end_time = min(current_time + word_duration, seg.end_time)
                new_segments.append(
                    ASRDataSeg(
                        text=word,
                        start_time=current_time,
                        end_time=word_end_time,
                        speaker=seg.speaker,
                    )
                )

                current_time = word_end_time

        self.segments = new_segments
        return self

    def remove_punctuation(self) -> "ASRData":
        """
        移除字幕中的标点符号(中文逗号、句号)
        """
        punctuation = r"[，。]"
        for seg in self.segments:

            seg.text = re.sub(f"{punctuation}+$", "", seg.text.strip())
            seg.translated_text = re.sub(
                f"{punctuation}+$", "", seg.translated_text.strip()
            )
        return self

    def remove_translated_periods(self) -> "ASRData":
        """仅删除译文中的全角句号，保留半角句号和其他文本内容。"""
        for seg in self.segments:
            translated = seg.translated_text or ""
            if not translated:
                continue
            seg.translated_text = translated.replace("。", "")
        return self

    def remove_translated_chinese_commas(self) -> "ASRData":
        """将译文中的中文逗号和英文逗号替换为空格"""
        for seg in self.segments:
            translated = (seg.translated_text or "").strip()
            if not translated:
                continue
            translated = translated.replace("，", " ").replace(",", " ")
            seg.translated_text = re.sub(r"\s+", " ", translated).strip()
        return self

    def normalize_translated_cjk_quotes(self) -> "ASRData":
        """将译文引号统一为外层「」和单引号『』。"""
        for seg in self.segments:
            seg.translated_text = normalize_cjk_quotes(seg.translated_text)
        return self

    def mask_original_profanity(self) -> "ASRData":
        """屏蔽原文字幕中的英文脏话，不处理译文。"""
        for seg in self.segments:
            seg.text = mask_english_profanity(seg.text)
        return self

    def save(self, save_path: str, layout: str = "原文在上") -> None:
        """
        Save the ASRData to a file

        Args:
            save_path: 保存路径
            layout: 字幕布局,可选值["原文在上", "译文在上", "仅原文", "仅译文"]
        """
        # 创建目录
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        if layout == SEPARATE_ORIGINAL_TRANSLATE_LAYOUT:
            self._save_separate_original_and_translated(save_path)
            return

        if save_path.endswith(".srt"):
            self.to_srt(save_path=save_path, layout=layout, include_speaker=True)
        elif save_path.endswith(".txt"):
            self.to_txt(save_path=save_path, layout=layout, include_speaker=True)
        elif save_path.endswith(".json"):
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(self.to_json(), f, ensure_ascii=False)
        else:
            raise ValueError(f"Unsupported file extension: {save_path}")

    def _save_with_layout(self, save_path: str, layout: str) -> None:
        if save_path.endswith(".srt"):
            self.to_srt(save_path=save_path, layout=layout, include_speaker=True)
        elif save_path.endswith(".txt"):
            self.to_txt(save_path=save_path, layout=layout, include_speaker=True)
        elif save_path.endswith(".json"):
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(self.to_json(), f, ensure_ascii=False)
        else:
            raise ValueError(f"Unsupported file extension: {save_path}")

    def _save_separate_original_and_translated(self, save_path: str) -> None:
        target_path = Path(save_path)
        original_path = target_path.with_name(
            f"{target_path.stem}-仅原文{target_path.suffix}"
        )
        translated_path = target_path.with_name(
            f"{target_path.stem}-仅译文{target_path.suffix}"
        )

        # Keep the primary output path valid for downstream steps, while also
        # exporting explicit original/translated single-language files.
        self._save_with_layout(str(target_path), "仅译文")
        self._save_with_layout(str(original_path), "仅原文")
        if translated_path != target_path:
            self._save_with_layout(str(translated_path), "仅译文")

    @staticmethod
    def _speaker_prefix(speaker: str) -> str:
        return f"[{speaker}] "

    def _apply_speaker_label(
        self,
        seg: ASRDataSeg,
        original: str,
        translated: str,
        last_speaker: Optional[str],
    ) -> Tuple[str, str, Optional[str]]:
        speaker = (seg.speaker or "").strip()
        if not speaker or speaker == last_speaker:
            return original, translated, last_speaker

        prefix = self._speaker_prefix(speaker)
        if original:
            original = f"{prefix}{original}"
        elif translated:
            translated = f"{prefix}{translated}"

        return original, translated, speaker

    def to_txt(
        self,
        save_path=None,
        layout: str = "原文在上",
        include_speaker: bool = False,
    ) -> str:
        """Convert to plain text subtitle format (without timestamps)"""
        result = []
        last_speaker = None
        for seg in self.segments:
            # 检查是否有换行符
            original = seg.text
            translated = seg.translated_text
            if include_speaker:
                original, translated, last_speaker = self._apply_speaker_label(
                    seg, original, translated, last_speaker
                )

            # 根据字幕类型组织文本
            if layout == "原文在上":
                text = f"{original}\n{translated}" if translated else original
            elif layout == "译文在上":
                text = f"{translated}\n{original}" if translated else original
            elif layout == "仅原文":
                text = original
            elif layout in ["仅译文", SEPARATE_ORIGINAL_TRANSLATE_LAYOUT]:
                text = translated if translated else original
            else:
                text = seg.transcript
            result.append(text)
        text = "\n".join(result)
        if save_path:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write("\n".join(result))
        return text

    def to_srt(
        self,
        layout: str = "原文在上",
        save_path=None,
        include_speaker: bool = False,
    ) -> str:
        """Convert to SRT subtitle format"""
        srt_lines = []
        last_speaker = None
        for n, seg in enumerate(self.segments, 1):
            # 检查是否有换行符
            original = seg.text
            translated = seg.translated_text
            if include_speaker:
                original, translated, last_speaker = self._apply_speaker_label(
                    seg, original, translated, last_speaker
                )

            # 根据字幕类型组织文本
            if layout == "原文在上":
                text = f"{original}\n{translated}" if translated else original
            elif layout == "译文在上":
                text = f"{translated}\n{original}" if translated else original
            elif layout == "仅原文":
                text = original
            elif layout in ["仅译文", SEPARATE_ORIGINAL_TRANSLATE_LAYOUT]:
                text = translated if translated else original
            else:
                text = seg.transcript

            srt_lines.append(f"{n}\n{seg.to_srt_ts()}\n{text}\n")

        srt_text = "\n".join(srt_lines)
        if save_path:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(srt_text)
        return srt_text

    def to_lrc(self, save_path=None) -> str:
        """Convert to LRC subtitle format"""
        raise NotImplementedError("LRC format is not supported")

    def to_json(self) -> dict:
        result_json = {}
        for i, segment in enumerate(self.segments, 1):
            # 检查是否有换行符
            original = segment.text
            translated = segment.translated_text

            result_json[str(i)] = {
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "original_subtitle": original,
                "translated_subtitle": translated,
                "speaker": segment.speaker,
            }
        return result_json

    def to_vtt(self, save_path=None) -> str:
        """转换为WebVTT字幕格式

        Args:
            save_path: 可选的保存路径

        Returns:
            str: WebVTT格式的字幕内容
        """
        raise NotImplementedError("WebVTT format is not supported")
        # # WebVTT头部
        # vtt_lines = ["WEBVTT\n"]

        # for n, seg in enumerate(self.segments, 1):
        #     # 转换时间戳格式从毫秒到 HH:MM:SS.mmm
        #     start_time = seg._ms_to_srt_time(seg.start_time).replace(",", ".")
        #     end_time = seg._ms_to_srt_time(seg.end_time).replace(",", ".")

        #     # 添加序号（可选）和时间戳
        #     vtt_lines.append(f"{n}\n{start_time} --> {end_time}\n{seg.transcript}\n")

        # vtt_text = "\n".join(vtt_lines)

        # if save_path:
        #     with open(save_path, "w", encoding="utf-8") as f:
        #         f.write(vtt_text)

        # return vtt_text

    def merge_segments(self, start_index: int, end_index: int, merged_text: str | None = None):
        """合并从 start_index 到 end_index 的段（包含）。"""
        if (
            start_index < 0
            or end_index >= len(self.segments)
            or start_index > end_index
        ):
            raise IndexError("无效的段索引。")
        merged_start_time = self.segments[start_index].start_time
        merged_end_time = self.segments[end_index].end_time
        if merged_text is None:
            merged_text = "".join(
                seg.text for seg in self.segments[start_index : end_index + 1]
            )
        merged_seg = ASRDataSeg(
            merged_text,
            merged_start_time,
            merged_end_time,
            speaker=merge_speakers(self.segments[start_index : end_index + 1]),
        )
        # 替换 segments[start_index:end_index+1] 为 merged_seg
        self.segments[start_index : end_index + 1] = [merged_seg]

    def merge_with_next_segment(self, index: int) -> None:
        """合并指定索引的段与下一个段。"""
        if index < 0 or index >= len(self.segments) - 1:
            raise IndexError("索引超出范围或没有下一个段可合并。")
        current_seg = self.segments[index]
        next_seg = self.segments[index + 1]
        merged_text = f"{current_seg.text} {next_seg.text}"
        merged_seg = ASRDataSeg(
            merged_text,
            current_seg.start_time,
            next_seg.end_time,
            speaker=merge_speakers([current_seg, next_seg]),
        )
        self.segments[index] = merged_seg
        # 删除下一个段
        del self.segments[index + 1]

    def optimize_timing(self, threshold_ms: int = 1000) -> "ASRData":
        """优化字幕显示时间，如果相邻字幕段之间的时间间隔小于阈值，
        则将交界点设置为两段字幕的中间时间点

        Args:
            threshold_ms: 时间间隔阈值(毫秒)，默认800ms

        Returns:
            返回自身以支持链式调用
        """
        if self.is_word_timestamp():
            return self

        if not self.segments:
            return self

        for i in range(len(self.segments) - 1):
            current_seg = self.segments[i]
            next_seg = self.segments[i + 1]

            # 计算时间间隔
            time_gap = next_seg.start_time - current_seg.end_time

            # 如果间隔小于阈值，将交界点设置为 3/4 时间点
            if time_gap < threshold_ms:
                mid_time = (
                    current_seg.end_time + next_seg.start_time
                ) // 2 + time_gap // 4
                current_seg.end_time = mid_time
                next_seg.start_time = mid_time

        return self

    def __str__(self):
        return self.to_txt()

    @staticmethod
    def from_subtitle_file(file_path: str) -> "ASRData":
        """从文件路径加载ASRData实例

        Args:
            file_path: 字幕文件路径，支持.srt、.vtt、.json格式

        Returns:
            ASRData: 解析后的ASRData实例

        Raises:
            ValueError: 不支持的文件格式或文件读取错误
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="gbk")

        suffix = path.suffix.lower()

        if suffix == ".srt":
            return ASRData.from_srt(content)
        elif suffix == ".vtt":
            if "<c>" in content and re.search(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", content):  # 字级时间戳
                return ASRData.from_youtube_vtt(content)
            return ASRData.from_vtt(content)
        elif suffix == ".json":
            return ASRData.from_json(json.loads(content))
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")

    @staticmethod
    def from_json(json_data: dict) -> "ASRData":
        """从JSON数据创建ASRData实例"""
        segments = []
        for i in sorted(json_data.keys(), key=int):
            segment_data = json_data[i]
            segment = ASRDataSeg(
                text=segment_data["original_subtitle"],
                translated_text=segment_data["translated_subtitle"],
                start_time=segment_data["start_time"],
                end_time=segment_data["end_time"],
                speaker=segment_data.get("speaker", ""),
            )
            segments.append(segment)
        return ASRData(segments)

    @staticmethod
    def from_srt(srt_str: str) -> "ASRData":
        """
        从SRT格式的字符串创建ASRData实例。

        :param srt_str: 包含SRT格式字幕的字符串。
        :return: 解析后的ASRData实例。
        """
        segments = []
        srt_time_pattern = re.compile(
            r"(\d{2}):(\d{2}):(\d{1,2})[.,](\d{3})\s-->\s(\d{2}):(\d{2}):(\d{1,2})[.,](\d{3})"
        )
        blocks = re.split(r"\n\s*\n", srt_str.strip())

        # 如果超过96%的块都超过4行，说明可能包含翻译文本
        blocks_lines_count = [len(block.splitlines()) for block in blocks]
        if (
            len(blocks_lines_count) > 0
            and all(count <= 4 for count in blocks_lines_count)
            and sum(count == 4 for count in blocks_lines_count)
            / len(blocks_lines_count)
            >= 0.98
        ):
            has_translated_subtitle = True
        else:
            has_translated_subtitle = False

        for block in blocks:
            lines = block.splitlines()
            if len(lines) < 3:  # 至少需要3行：序号、时间戳和文本
                continue

            match = srt_time_pattern.match(lines[1])
            if not match:
                continue

            time_parts = list(map(int, match.groups()))
            start_time = sum(
                [
                    time_parts[0] * 3600000,
                    time_parts[1] * 60000,
                    time_parts[2] * 1000,
                    time_parts[3],
                ]
            )
            end_time = sum(
                [
                    time_parts[4] * 3600000,
                    time_parts[5] * 60000,
                    time_parts[6] * 1000,
                    time_parts[7],
                ]
            )

            if has_translated_subtitle and len(lines) >= 4:
                text = lines[2]
                translated_text = lines[3]
                segments.append(
                    ASRDataSeg(
                        text, start_time, end_time, translated_text=translated_text
                    )
                )
            else:
                text = lines[2]
                segments.append(ASRDataSeg(text, start_time, end_time))

        return ASRData(segments)

    @staticmethod
    def from_vtt(vtt_str: str) -> "ASRData":
        """
        从 VTT 格式的字符串创建ASRData实例。

        :param vtt_str: VTT格式的字幕字符串
        :return: ASRData实例
        """
        segments = []
        timestamp = r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}"
        timing = re.compile(rf"^({timestamp})\s+-->\s+({timestamp})(?:\s+.*)?$")

        def milliseconds(value: str) -> int:
            parts = value.split(":")
            seconds, fraction = parts[-1].split(".")
            hours = int(parts[0]) if len(parts) == 3 else 0
            return hours * 3600000 + int(parts[-2]) * 60000 + int(seconds) * 1000 + int(fraction)

        normalized = vtt_str.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        for block in re.split(r"\n[ \t]*\n+", normalized.strip()):
            lines = block.strip().splitlines()
            if not lines or re.match(r"^(?:WEBVTT|NOTE|STYLE|REGION)(?:\s|$)", lines[0]):
                continue
            # A cue identifier is optional; settings follow the end timestamp.
            for index in range(min(2, len(lines))):
                match = timing.match(lines[index].strip())
                if match:
                    text = " ".join(lines[index + 1:])
                    text = html.unescape(re.sub(r"<[^>]*>", "", text))
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        segments.append(ASRDataSeg(text, milliseconds(match[1]), milliseconds(match[2])))
                    break
        return ASRData(segments)

    @staticmethod
    def from_youtube_vtt(vtt_str: str) -> "ASRData":
        """
        从YouTube VTT格式的字符串创建ASRData实例，提取字级时间戳。

        :param vtt_str: 包含VTT格式字幕的字符串
        :return: 解析后的ASRData实例
        """

        def parse_timestamp(ts: str) -> int:
            """将时间戳字符串转换为毫秒"""
            h, m, s = ts.split(":")
            return int(float(h) * 3600000 + float(m) * 60000 + float(s) * 1000)

        def split_timestamped_text(text: str) -> List[ASRDataSeg]:
            """分离带时间戳的文本为单词段"""
            pattern = re.compile(r"<(\d{2}:\d{2}:\d{2}\.\d{3})>([^<]*)")
            matches = list(pattern.finditer(text))
            word_segments = []

            for i in range(len(matches) - 1):
                current_match = matches[i]
                next_match = matches[i + 1]

                start_time = parse_timestamp(current_match.group(1))
                end_time = parse_timestamp(next_match.group(1))
                word = current_match.group(2).strip()

                if word:
                    word_segments.append(ASRDataSeg(word, start_time, end_time))

            return word_segments

        segments = []
        blocks = re.split(r"\n\n+", vtt_str.strip())

        timestamp_pattern = re.compile(
            r"(\d{2}):(\d{2}):(\d{2}\.\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}\.\d{3})"
        )
        for block in blocks:
            lines = block.strip().split("\n")
            if not lines:
                continue

            match = timestamp_pattern.match(lines[0])
            if not match:
                continue

            block_start_time = (
                int(match.group(1)) * 3600000
                + int(match.group(2)) * 60000
                + float(match.group(3)) * 1000
            )
            block_end_time = (
                int(match.group(4)) * 3600000
                + int(match.group(5)) * 60000
                + float(match.group(6)) * 1000
            )

            # 获取文本内容
            text = "\n".join(lines)

            timestamp_row = re.search(r"\n(.*?<c>.*?</c>.*)", block)
            if timestamp_row:
                text = re.sub(r"<c>|</c>", "", timestamp_row.group(1))
                block_start_time_string = (
                    f"{match.group(1)}:{match.group(2)}:{match.group(3)}"
                )
                block_end_time_string = (
                    f"{match.group(4)}:{match.group(5)}:{match.group(6)}"
                )
                text = f"<{block_start_time_string}>{text}<{block_end_time_string}>"

                # 分离每个带时间戳的单词
                word_segments = split_timestamped_text(text)
                segments.extend(word_segments)

        return ASRData(segments)
