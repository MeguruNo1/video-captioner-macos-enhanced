from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    ComboBoxSettingCard,
    InfoBar,
    MessageBoxBase,
    PushButton,
    PushSettingCard,
    SettingCardGroup,
    SwitchSettingCard,
    TextEdit,
)
from qfluentwidgets import FluentIcon as FIF

from ..common.config import cfg
from ..core.entities import TranscribeLanguageEnum
from ..core.subtitle_processor.prompt import PROMPT_TERM_GLOSSARY, get_prompt_template
from ..core.utils.mlx_model_utils import (
    DEFAULT_LOCAL_MLX_MODEL_DIR,
    DEFAULT_MLX_MODEL,
    discover_local_mlx_models,
    is_valid_local_mlx_model,
    preferred_mlx_model,
    validate_mlx_model,
)
from ..core.utils.transcription_model_utils import resolve_available_mlx_model
from ..core.utils.transcript_file_locator import resolve_default_transcript_path
from ..core.utils.transcript_terms import (
    apply_terms_to_document_prompt,
    apply_terms_to_mlx_hotwords,
    extract_translation_terms_from_hotwords,
    parse_hotwords_text,
)
from .EditComboBoxSettingCard import EditComboBoxSettingCard
from .LineEditSettingCard import LineEditSettingCard
from .SpinBoxSettingCard import DoubleSpinBoxSettingCard, SpinBoxSettingCard
from .WhisperXSettingWidget import HotwordExtractionThread


class MLXHotwordsDialog(MessageBoxBase):
    def __init__(self, parent=None, default_transcript_path: str = ""):
        super().__init__(parent)
        self.hotwordExtractionThread = None
        self.defaultTranscriptPath = default_transcript_path
        self.setWindowTitle(self.tr("MLX Whisper 热词提示"))
        self.widget.setMinimumWidth(760)
        self.widget.setMaximumWidth(980)

        self.titleLabel = BodyLabel(self.tr("MLX Whisper 热词提示"), self)
        self.descLabel = BodyLabel(
            self.tr("热词会与初始提示词合并后传给 MLX Whisper 的 initial_prompt。"),
            self,
        )
        self.descLabel.setWordWrap(True)
        self.hotwordsEdit = TextEdit(self)
        self.hotwordsEdit.setMinimumSize(680, 420)
        self.hotwordsEdit.setPlainText(cfg.mlx_hotwords.value)

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
        cfg.set(cfg.mlx_hotwords, self.hotwordsEdit.toPlainText().strip())
        InfoBar.success(
            self.tr("已保存"),
            self.tr("MLX Whisper 热词提示已更新"),
            duration=2500,
            parent=self,
        )
        return True

    def _target_language(self) -> str:
        value = cfg.target_language.value
        return str(getattr(value, "value", value) or "")

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
            result = apply_terms_to_mlx_hotwords(terms)
            self.hotwordsEdit.setPlainText(result["mlx_hotwords"])
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
                self.tr("请先填写或提取 MLX Whisper 热词"),
                duration=3000,
                parent=self,
            )
            return

        try:
            cfg.set(cfg.mlx_hotwords, hotwords)
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


class MLXWhisperSettingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.setting_group = SettingCardGroup(
            self.tr("MLX Whisper 设置（Apple Silicon GPU）"),
            self,
        )

        self.model_card = EditComboBoxSettingCard(
            cfg.mlx_model,
            FIF.ROBOT,
            self.tr("模型"),
            self.tr("选择或输入 MLX Whisper 模型名称或本地模型目录"),
            self._model_options(),
            self.setting_group,
        )
        self.local_model_card = PushSettingCard(
            self.tr("选择"),
            FIF.FOLDER,
            self.tr("本地模型目录"),
            self.tr("选择包含 config.json 和权重文件的 MLX Whisper 模型目录"),
            self.setting_group,
        )
        self.model_status_card = PushSettingCard(
            self.tr("检测"),
            FIF.INFO,
            self.tr("模型接入状态"),
            self.tr("未检测"),
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

        self.word_timestamps_card = SwitchSettingCard(
            FIF.UNIT,
            self.tr("词级时间轴"),
            self.tr("开启后使用 MLX 转录，并由 WhisperX 声学模型强制对齐"),
            cfg.mlx_word_timestamps,
            self.setting_group,
        )

        self.vad_enabled_card = SwitchSettingCard(
            FIF.SPEED_HIGH,
            self.tr("VAD 语音检测"),
            self.tr("开启后先检测语音区间，再交给 MLX Whisper 转录"),
            cfg.mlx_vad_enabled,
            self.setting_group,
        )

        self.vad_threshold_card = DoubleSpinBoxSettingCard(
            cfg.mlx_vad_threshold,
            FIF.SPEED_HIGH,
            self.tr("VAD 阈值"),
            self.tr("数值越高越严格；检测失败时会自动回退到普通分块"),
            minimum=0.1,
            maximum=0.9,
            decimals=2,
            step=0.05,
            parent=self.setting_group,
        )

        self.chunk_duration_card = SpinBoxSettingCard(
            cfg.mlx_chunk_duration,
            FIF.SPEED_HIGH,
            self.tr("分块时长（秒）"),
            self.tr("长音频会按该时长分块转录并合并全局时间轴"),
            minimum=60,
            maximum=1800,
            parent=self.setting_group,
        )

        self.chunk_overlap_card = SpinBoxSettingCard(
            cfg.mlx_chunk_overlap,
            FIF.SPEED_HIGH,
            self.tr("分块重叠（秒）"),
            self.tr("相邻分块保留重叠区以减少切点漏字"),
            minimum=0,
            maximum=300,
            parent=self.setting_group,
        )

        self.hotwords_card = PushSettingCard(
            self.tr("管理"),
            FIF.CHAT,
            self.tr("热词提示"),
            self._hotwords_summary(),
            self.setting_group,
        )

        self.initial_prompt_card = LineEditSettingCard(
            cfg.mlx_initial_prompt,
            FIF.DOCUMENT,
            self.tr("初始提示词"),
            self.tr("给 MLX Whisper 的可选上下文，例如语言、场景、专有名词和标点风格"),
            "",
            self.setting_group,
        )

        self.model_card.comboBox.setMinimumWidth(280)
        self.language_card.comboBox.setMinimumWidth(200)
        self.initial_prompt_card.lineEdit.setMinimumWidth(200)

        self.setting_group.addSettingCard(self.model_card)
        self.setting_group.addSettingCard(self.local_model_card)
        self.setting_group.addSettingCard(self.model_status_card)
        self.setting_group.addSettingCard(self.language_card)
        self.setting_group.addSettingCard(self.word_timestamps_card)
        self.setting_group.addSettingCard(self.vad_enabled_card)
        self.setting_group.addSettingCard(self.vad_threshold_card)
        self.setting_group.addSettingCard(self.chunk_duration_card)
        self.setting_group.addSettingCard(self.chunk_overlap_card)
        self.setting_group.addSettingCard(self.hotwords_card)
        self.setting_group.addSettingCard(self.initial_prompt_card)

        self.main_layout.addWidget(self.setting_group)

        self.hotwords_card.clicked.connect(self.__on_hotwords_clicked)
        self.local_model_card.clicked.connect(self.__on_local_model_clicked)
        self.model_status_card.clicked.connect(
            lambda: self.refresh_model_status(show_warning=True)
        )
        self.model_card.currentTextChanged.connect(lambda _: self.refresh_model_status())
        self.__use_preferred_local_model_if_available()
        self.refresh_model_status()

    def _model_options(self) -> list[str]:
        remote_models = [
            DEFAULT_MLX_MODEL,
            "mlx-community/whisper-large-v3-mlx",
            "mlx-community/distil-whisper-large-v3",
            "mlx-community/whisper-medium",
            "mlx-community/whisper-small",
            "mlx-community/whisper-base",
            "mlx-community/whisper-tiny",
        ]
        local_models = discover_local_mlx_models()
        return local_models + [
            model for model in remote_models if model not in local_models
        ]

    def __use_preferred_local_model_if_available(self):
        model = preferred_mlx_model(cfg.mlx_model.value)
        if model != cfg.mlx_model.value:
            cfg.set(cfg.mlx_model, model)
            self.model_card.setValue(model)
        self.model_card.setItems(self._model_options())
        self.model_card.setValue(cfg.mlx_model.value)

    def __on_local_model_clicked(self):
        current_model = str(cfg.mlx_model.value or "").strip()
        default_dir = (
            current_model
            if is_valid_local_mlx_model(current_model)
            else str(DEFAULT_LOCAL_MLX_MODEL_DIR.parent)
        )
        folder = QFileDialog.getExistingDirectory(
            self,
            self.tr("选择 MLX Whisper 本地模型目录"),
            default_dir,
        )
        if not folder:
            return

        if not is_valid_local_mlx_model(folder):
            InfoBar.warning(
                self.tr("模型目录不可用"),
                self.tr("请选择包含 config.json 和权重文件（weights.safetensors 或 weights.npz）的目录。"),
                duration=5000,
                parent=self,
            )
            self.model_status_card.setContent(
                self.tr("目录缺少 config.json 或权重文件（weights.safetensors / weights.npz）")
            )
            return

        cfg.set(cfg.mlx_model, folder)
        self.model_card.setItems(self._model_options())
        self.model_card.setValue(folder)
        self.refresh_model_status()
        InfoBar.success(
            self.tr("已接入本地模型"),
            folder,
            duration=3500,
            parent=self,
        )

    def refresh_model_status(self, show_warning: bool = False):
        model = str(cfg.mlx_model.value or "").strip()
        is_valid_model, message = validate_mlx_model(model)
        ready_path = resolve_available_mlx_model(model) if is_valid_model else None
        if ready_path is not None:
            message = self.tr("模型已就绪：") + str(ready_path)
        elif is_valid_model:
            is_valid_model = False
            message = self.tr(
                "模型尚未下载完成；请选择本地模型目录，或先完成模型下载"
            )
        self.model_status_card.setContent(self.tr(message))
        self.model_status_card.setToolTip(self.tr(message))
        if not show_warning:
            return
        if is_valid_model:
            InfoBar.success(
                self.tr("MLX Whisper 模型可用"),
                self.tr(message),
                duration=4000,
                parent=self,
            )
        else:
            InfoBar.warning(
                self.tr("MLX Whisper 模型未接入"),
                self.tr(message),
                duration=5000,
                parent=self,
            )

    def _hotwords_summary(self) -> str:
        hotwords = parse_hotwords_text(cfg.mlx_hotwords.value)
        if not hotwords:
            return self.tr("未设置")
        preview = ", ".join(hotwords[:3])
        if len(hotwords) > 3:
            preview += self.tr(" 等 {0} 条").format(len(hotwords))
        return preview

    def __on_hotwords_clicked(self):
        dialog = MLXHotwordsDialog(
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
