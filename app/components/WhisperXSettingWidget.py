from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import ComboBoxSettingCard, PushSettingCard
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    MessageBoxBase,
    PushButton,
    RangeSettingCard,
    SettingCardGroup,
    TextEdit,
)
from qfluentwidgets import SwitchSettingCard

from app.config import WHISPERX_ONLY_MODE

from ..common.config import cfg
from ..core.bk_asr.asr_data import ASRData
from ..core.entities import TranscribeLanguageEnum
from ..core.subtitle_processor.prompt import PROMPT_TERM_GLOSSARY, get_prompt_template
from ..core.utils.transcript_terms import (
    apply_terms_to_document_prompt,
    apply_terms_to_whisperx_hotwords,
    extract_terms_with_ai,
    extract_translation_terms_from_hotwords,
    parse_hotwords_text,
)
from ..core.utils.transcript_file_locator import resolve_default_transcript_path
from .EditComboBoxSettingCard import EditComboBoxSettingCard
from .LineEditSettingCard import LineEditSettingCard
from .SpinBoxSettingCard import DoubleSpinBoxSettingCard


class HotwordExtractionThread(QThread):
    status_changed = pyqtSignal(str)
    succeeded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        file_path: str,
        glossary_text: str,
        target_language: str,
        parent=None,
    ):
        super().__init__(parent)
        self.file_path = file_path
        self.glossary_text = glossary_text
        self.target_language = target_language

    def run(self):
        try:
            self.status_changed.emit("正在读取文稿...")
            transcript_text = self._read_text_file(self.file_path)
            if not transcript_text.strip():
                raise ValueError("文稿内容为空，无法提取热词")

            self.status_changed.emit("正在调用 AI 提取热词，可能需要几十秒...")
            terms = extract_terms_with_ai(
                transcript_text,
                self.glossary_text,
                self.target_language,
            )
            self.succeeded.emit(terms)
        except Exception as exc:
            self.failed.emit(str(exc))

    @staticmethod
    def _read_text_file(file_path: str) -> str:
        suffix = Path(file_path).suffix.lower()
        if suffix in {".srt", ".vtt", ".json"}:
            return ASRData.from_subtitle_file(file_path).to_txt(layout="仅原文")
        return Path(file_path).read_text(encoding="utf-8-sig")


class WhisperXHotwordsDialog(MessageBoxBase):
    def __init__(self, parent=None, default_transcript_path: str = ""):
        super().__init__(parent)
        self.hotwordExtractionThread = None
        self.defaultTranscriptPath = default_transcript_path
        self.setWindowTitle(self.tr("WhisperX 热词管理"))
        self.widget.setMinimumWidth(760)
        self.widget.setMaximumWidth(980)

        self.titleLabel = BodyLabel(self.tr("WhisperX 热词管理"), self)
        self.descLabel = BodyLabel(
            self.tr("热词用于 WhisperX 转录；生成翻译术语后会写入文稿提示。"),
            self,
        )
        self.descLabel.setWordWrap(True)
        self.hotwordsEdit = TextEdit(self)
        self.hotwordsEdit.setMinimumSize(680, 420)
        self.hotwordsEdit.setPlainText(cfg.whisperx_hotwords.value)

        actionWidget = QWidget(self)
        actionLayout = QHBoxLayout(actionWidget)
        actionLayout.setContentsMargins(0, 0, 0, 0)
        actionLayout.setSpacing(8)
        self.extractHotwordsButton = PushButton(self.tr("从视频文稿提取热词"), self)
        self.generateTermsButton = PushButton(self.tr("生成翻译术语"), self)
        actionLayout.addWidget(self.extractHotwordsButton)
        actionLayout.addWidget(self.generateTermsButton)
        actionLayout.addStretch(1)

        self.statusLabel = BodyLabel(self.tr("请选择文稿或字幕文件后提取热词"), self)
        self.statusLabel.setWordWrap(True)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descLabel)
        self.viewLayout.addWidget(self.hotwordsEdit)
        self.viewLayout.addWidget(actionWidget)
        self.viewLayout.addWidget(self.statusLabel)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("关闭"))

        self.extractHotwordsButton.clicked.connect(self.extract_hotwords_from_file)
        self.generateTermsButton.clicked.connect(self.generate_translation_terms)

    def validate(self) -> bool:
        cfg.set(cfg.whisperx_hotwords, self.hotwordsEdit.toPlainText().strip())
        InfoBar.success(
            self.tr("已保存"),
            self.tr("WhisperX 热词已更新"),
            duration=2500,
            parent=self,
        )
        return True

    def _target_language(self) -> str:
        value = cfg.target_language.value
        return str(getattr(value, "value", value) or "")

    def _read_text_file(self, file_path: str) -> str:
        return HotwordExtractionThread._read_text_file(file_path)

    def _select_transcript_file(self) -> str:
        initial_path = self.defaultTranscriptPath or str(cfg.work_dir.value)
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择视频文稿或字幕文件"),
            initial_path,
            self.tr("文稿或字幕 (*.txt *.srt *.vtt *.json);;所有文件 (*)"),
        )
        return file_path

    def extract_hotwords_from_file(self):
        if self.hotwordExtractionThread is not None:
            InfoBar.info(
                self.tr("正在提取"),
                self.tr("请等待当前热词提取完成"),
                duration=2500,
                parent=self,
            )
            return

        file_path = self._select_transcript_file()
        if not file_path:
            return

        self._set_hotword_extraction_running(
            True,
            self.tr("已选择文稿，准备提取热词..."),
        )
        InfoBar.info(
            self.tr("开始提取"),
            self.tr("正在读取文稿并调用 AI，请稍候"),
            duration=3000,
            parent=self,
        )

        self.hotwordExtractionThread = HotwordExtractionThread(
            file_path,
            get_prompt_template(PROMPT_TERM_GLOSSARY),
            self._target_language(),
            self,
        )
        self.hotwordExtractionThread.status_changed.connect(
            self._update_hotword_extraction_status
        )
        self.hotwordExtractionThread.succeeded.connect(self._on_hotwords_extracted)
        self.hotwordExtractionThread.failed.connect(self._on_hotword_extraction_failed)
        self.hotwordExtractionThread.finished.connect(
            self._on_hotword_extraction_finished
        )
        self.hotwordExtractionThread.start()

    def _set_hotword_extraction_running(self, running: bool, status: str = ""):
        self.extractHotwordsButton.setEnabled(not running)
        self.generateTermsButton.setEnabled(not running)
        self.hotwordsEdit.setEnabled(not running)
        self.yesButton.setEnabled(not running)
        self.cancelButton.setEnabled(not running)
        self.extractHotwordsButton.setText(
            self.tr("正在提取...") if running else self.tr("从视频文稿提取热词")
        )
        if status:
            self.statusLabel.setText(status)

    def _update_hotword_extraction_status(self, status: str):
        self.statusLabel.setText(self.tr(status))

    def _on_hotwords_extracted(self, terms: list):
        try:
            if not terms:
                self.statusLabel.setText(self.tr("提取完成，但 AI 未返回可用热词"))
                InfoBar.warning(
                    self.tr("未提取到热词"),
                    self.tr("AI 未返回可用名称或术语"),
                    duration=3000,
                    parent=self,
                )
                return

            self.statusLabel.setText(self.tr("正在覆盖热词并更新配置..."))
            result = apply_terms_to_whisperx_hotwords(terms)
            self.hotwordsEdit.setPlainText(result["whisperx_hotwords"])
            self.statusLabel.setText(
                self.tr("提取完成，已覆盖为 {0} 条候选热词").format(len(terms))
            )
            InfoBar.success(
                self.tr("提取完成"),
                self.tr("已覆盖为 {0} 条候选热词，旧翻译术语已清理").format(len(terms)),
                duration=3000,
                parent=self,
            )
        except Exception as exc:
            InfoBar.error(
                self.tr("提取失败"),
                str(exc),
                duration=5000,
                parent=self,
            )

    def _on_hotword_extraction_failed(self, error: str):
        self.statusLabel.setText(self.tr("提取失败：") + error)
        InfoBar.error(
            self.tr("提取失败"),
            error,
            duration=5000,
            parent=self,
        )

    def _on_hotword_extraction_finished(self):
        if self.hotwordExtractionThread is not None:
            self.hotwordExtractionThread.deleteLater()
            self.hotwordExtractionThread = None
        self._set_hotword_extraction_running(False)

    def generate_translation_terms(self):
        hotwords = self.hotwordsEdit.toPlainText().strip()
        if not parse_hotwords_text(hotwords):
            InfoBar.warning(
                self.tr("热词为空"),
                self.tr("请先填写或提取 WhisperX 热词"),
                duration=3000,
                parent=self,
            )
            return

        try:
            cfg.set(cfg.whisperx_hotwords, hotwords)
            terms = extract_translation_terms_from_hotwords(
                hotwords,
                self._target_language(),
                get_prompt_template(PROMPT_TERM_GLOSSARY),
            )
            if not terms:
                InfoBar.warning(
                    self.tr("未生成术语"),
                    self.tr("AI 未返回可用翻译术语"),
                    duration=3000,
                    parent=self,
                )
                return

            apply_terms_to_document_prompt(terms)
            InfoBar.success(
                self.tr("术语已生成"),
                self.tr("已覆盖写入 {0} 条翻译术语到文稿提示").format(len(terms)),
                duration=3500,
                parent=self,
            )
        except Exception as exc:
            InfoBar.error(
                self.tr("生成失败"),
                str(exc),
                duration=5000,
                parent=self,
            )


class WhisperXSettingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        if WHISPERX_ONLY_MODE:
            cfg.set(cfg.whisperx_word_timestamps, True)
            cfg.set(cfg.whisperx_align, True)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.setting_group = SettingCardGroup(
            self.tr("WhisperX 设置（自动 / CUDA / CPU）")
            if WHISPERX_ONLY_MODE
            else self.tr("WhisperX 设置（需本地 Python 依赖）"),
            self,
        )

        self.model_card = EditComboBoxSettingCard(
            cfg.whisperx_model,
            FIF.ROBOT,
            self.tr("模型"),
            self.tr("选择或输入 WhisperX 模型名称"),
            ["large-v3", "large-v3-turbo", "medium", "small", "base"],
            self.setting_group,
        )

        self.language_card = ComboBoxSettingCard(
            cfg.transcribe_language,
            FIF.LANGUAGE,
            self.tr("源语言"),
            self.tr("音频的源语言"),
            [lang.value for lang in TranscribeLanguageEnum],
            self.setting_group,
        )

        self.auto_language_card = SwitchSettingCard(
            FIF.SEARCH,
            self.tr("自动检测语言"),
            self.tr("由 WhisperX 自动识别音频语言"),
            cfg.whisperx_auto_language,
            self.setting_group,
        )

        self.device_card = ComboBoxSettingCard(
            cfg.whisperx_device,
            FIF.IOT,
            self.tr("运行设备"),
            self.tr("auto 检查运行环境后优先使用 CUDA，否则使用 CPU"),
            ["auto", "cuda", "cpu"],
            self.setting_group,
        )

        self.compute_type_card = EditComboBoxSettingCard(
            cfg.whisperx_compute_type,
            FIF.ROBOT,
            self.tr("计算精度"),
            self.tr("设置 WhisperX 的计算精度"),
            ["auto", "float16", "int8", "int8_float16", "int8_float32", "float32"],
            self.setting_group,
        )

        self.batch_size_card = RangeSettingCard(
            cfg.whisperx_batch_size,
            FIF.SPEED_HIGH,
            self.tr("批大小"),
            self.tr("WhisperX 转录批大小"),
            parent=self.setting_group,
        )

        self.hotwords_card = PushSettingCard(
            self.tr("管理"),
            FIF.CHAT,
            self.tr("热词管理"),
            self._hotwords_summary(),
            self.setting_group,
        )

        self.initial_prompt_card = LineEditSettingCard(
            cfg.whisperx_initial_prompt,
            FIF.DOCUMENT,
            self.tr("初始提示词"),
            self.tr("给 WhisperX 的可选上下文，例如语言、场景、专有名词和标点风格"),
            "",
            self.setting_group,
        )

        self.vad_method_card = ComboBoxSettingCard(
            cfg.whisperx_vad_method,
            FIF.MUSIC,
            self.tr("VAD 方法"),
            self.tr("语音活动检测后端"),
            ["silero", "pyannote"],
            self.setting_group,
        )

        self.vad_threshold_card = DoubleSpinBoxSettingCard(
            cfg.whisperx_vad_threshold,
            FIF.VOLUME,
            self.tr("VAD 阈值"),
            self.tr("语音检测触发阈值，数值越高越保守"),
            minimum=0.0,
            maximum=1.0,
            decimals=2,
            step=0.05,
            parent=self.setting_group,
        )

        local_silero_dir = cfg.get(cfg.whisperx_local_silero_dir) or self.tr("未设置")
        self.local_silero_dir_card = PushSettingCard(
            self.tr("选择"),
            FIF.FOLDER,
            self.tr("本地 Silero 仓库"),
            local_silero_dir,
            self.setting_group,
        )

        self.word_timestamps_card = SwitchSettingCard(
            FIF.UNIT,
            self.tr("词级时间轴"),
            self.tr("开启后生成词级时间戳，单句会更短，便于后续断句"),
            cfg.whisperx_word_timestamps,
            self.setting_group,
        )

        self.align_card = SwitchSettingCard(
            FIF.ALIGNMENT,
            self.tr("对齐校正"),
            self.tr("启用 WhisperX 对齐以获得更稳定的时间戳"),
            cfg.whisperx_align,
            self.setting_group,
        )

        self.language_card.comboBox.setMaxVisibleItems(6)
        self.model_card.comboBox.setMinimumWidth(200)
        self.language_card.comboBox.setMinimumWidth(200)
        self.device_card.comboBox.setMinimumWidth(200)
        self.compute_type_card.comboBox.setMinimumWidth(200)
        self.initial_prompt_card.lineEdit.setMinimumWidth(200)
        self.vad_method_card.comboBox.setMinimumWidth(200)

        self.setting_group.addSettingCard(self.model_card)
        self.setting_group.addSettingCard(self.language_card)
        self.setting_group.addSettingCard(self.auto_language_card)
        self.setting_group.addSettingCard(self.device_card)
        self.setting_group.addSettingCard(self.compute_type_card)
        self.setting_group.addSettingCard(self.batch_size_card)
        self.setting_group.addSettingCard(self.hotwords_card)
        self.setting_group.addSettingCard(self.initial_prompt_card)
        self.setting_group.addSettingCard(self.vad_method_card)
        self.setting_group.addSettingCard(self.vad_threshold_card)
        self.setting_group.addSettingCard(self.local_silero_dir_card)
        self.setting_group.addSettingCard(self.word_timestamps_card)
        self.setting_group.addSettingCard(self.align_card)

        if WHISPERX_ONLY_MODE:
            self.word_timestamps_card.setEnabled(False)
            self.align_card.setEnabled(False)

        self.main_layout.addWidget(self.setting_group)

        self.hotwords_card.clicked.connect(self.__on_hotwords_clicked)
        self.local_silero_dir_card.clicked.connect(self.__on_local_silero_dir_clicked)
        self.vad_method_card.comboBox.currentTextChanged.connect(
            self.__update_local_silero_dir_card_state
        )
        self.__update_local_silero_dir_card_state(
            self.vad_method_card.comboBox.currentText()
        )

    def __on_local_silero_dir_clicked(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            self.tr("选择 Silero 仓库文件夹"),
            cfg.get(cfg.whisperx_local_silero_dir) or "./",
        )
        if not folder:
            return

        cfg.set(cfg.whisperx_local_silero_dir, folder)
        self.local_silero_dir_card.setContent(folder)

    def _hotwords_summary(self) -> str:
        hotwords = parse_hotwords_text(cfg.whisperx_hotwords.value)
        if not hotwords:
            return self.tr("未设置")
        preview = ", ".join(hotwords[:3])
        if len(hotwords) > 3:
            preview += self.tr(" 等 {0} 条").format(len(hotwords))
        return preview

    def __on_hotwords_clicked(self):
        dialog = WhisperXHotwordsDialog(
            self.window(),
            default_transcript_path=self._default_transcript_path(),
        )
        dialog.exec_()
        self.hotwords_card.setContent(self._hotwords_summary())

    def _default_transcript_path(self) -> str:
        candidates = []
        window = self.window()

        download_interface = getattr(window, "downloadCenterInterface", None)
        result = getattr(download_interface, "last_result", None) or {}
        for key in (
            "transcript_txt_path",
            "subtitle_path",
            "work_dir",
            "video_path",
            "media_path",
            "original_video_path",
        ):
            value = result.get(key)
            if value:
                candidates.append(value)
        candidates.extend(result.get("media_paths") or [])

        home_interface = getattr(window, "homeInterface", None)
        transcription_interface = getattr(home_interface, "transcription_interface", None)
        if transcription_interface:
            video_info_card = getattr(transcription_interface, "video_info_card", None)
            for task in (
                getattr(video_info_card, "task", None),
                getattr(transcription_interface, "task", None),
            ):
                if task:
                    candidates.extend(
                        [
                            getattr(task, "output_path", None),
                            getattr(task, "file_path", None),
                        ]
                    )

        subtitle_interface = getattr(home_interface, "subtitle_optimization_interface", None)
        if subtitle_interface:
            task = getattr(subtitle_interface, "task", None)
            if task:
                candidates.extend(
                    [
                        getattr(task, "subtitle_path", None),
                        getattr(task, "video_path", None),
                        getattr(task, "output_path", None),
                    ]
                )
            candidates.append(getattr(subtitle_interface, "subtitle_path", None))

        return resolve_default_transcript_path(candidates, cfg.work_dir.value)

    def __update_local_silero_dir_card_state(self, vad_method: str):
        enabled = (vad_method or "") == "silero"
        self.local_silero_dir_card.setEnabled(enabled)
