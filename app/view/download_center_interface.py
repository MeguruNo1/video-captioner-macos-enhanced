# -*- coding: utf-8 -*-
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from PyQt5.QtCore import QPoint, Qt, QSize, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtWidgets import QSizePolicy
from PyQt5.QtWidgets import QBoxLayout
from qfluentwidgets import (
    Action,
    BodyLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    CommandBar,
    FluentIcon as FIF,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    IndeterminateProgressBar,
    LineEdit,
    MenuAnimationType,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SegmentedWidget,
    isDarkTheme,
)

from app.common.config import cfg
from app.config import APP_DATA_PATH
from app.core.entities import TranscribeModelEnum
from app.core.utils.desktop_notification import send_desktop_notification
from app.core.utils.platform_utils import open_path


class AspectRatioLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._aspect_ratio = 16 / 9

    def setPixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        if not pixmap.isNull():
            self._aspect_ratio = pixmap.width() / pixmap.height()
        self._update_scaled_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        if self._pixmap.isNull():
            super().setPixmap(QPixmap())
            return
        label_size = self.size()
        if label_size.width() <= 0 or label_size.height() <= 0:
            return
        scaled = self._pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        super().setPixmap(scaled)

    def heightForWidth(self, width: int) -> int:
        return int(width / self._aspect_ratio)

    def sizeHint(self):
        return QSize(240, int(240 / self._aspect_ratio))

    def hasHeightForWidth(self):
        return True


class GroupedSubtitleComboBox(ComboBox):
    """Combo box that draws native separators between subtitle source groups."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.separator_before_indices: set[int] = set()

    def set_separator_before(self, indices) -> None:
        self.separator_before_indices = {int(index) for index in indices}

    def _showComboMenu(self):
        if not self.items:
            return

        menu = self._createComboMenu()
        actions = []
        for index, item in enumerate(self.items):
            if index in self.separator_before_indices:
                menu.addSeparator()
            action = QAction(
                item.icon,
                item.text,
                triggered=lambda _checked=False, item_index=index: self._onItemClicked(
                    item_index
                ),
            )
            action.setEnabled(item.isEnabled)
            menu.addAction(action)
            actions.append(action)

        if menu.view.width() < self.width():
            menu.view.setMinimumWidth(self.width())
            menu.adjustSize()

        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu
        if self.currentIndex() >= 0 and actions:
            menu.setDefaultAction(actions[self.currentIndex()])

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        drop_down_position = self.mapToGlobal(QPoint(x, self.height()))
        drop_down_height = menu.view.heightForAnimation(
            drop_down_position, MenuAnimationType.DROP_DOWN
        )
        pull_up_position = self.mapToGlobal(QPoint(x, 0))
        pull_up_height = menu.view.heightForAnimation(
            pull_up_position, MenuAnimationType.PULL_UP
        )
        if drop_down_height >= pull_up_height:
            menu.view.adjustSize(drop_down_position, MenuAnimationType.DROP_DOWN)
            menu.exec(drop_down_position, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.view.adjustSize(pull_up_position, MenuAnimationType.PULL_UP)
            menu.exec(pull_up_position, aniType=MenuAnimationType.PULL_UP)


class DownloadCenterInterface(QWidget):
    send_to_transcription = pyqtSignal(str)
    DOWNLOAD_STATE_PATH = APP_DATA_PATH / "download_center_state.json"
    COMPACT_CONTENT_WIDTH = 640
    PR_SUPPORTED_AUDIO_EXTS = {"aac", "aif", "aiff", "bwf", "m4a", "mp3", "mp4", "wav"}
    PR_SMART_PREFERRED_AUDIO_EXTS = {"aac", "m4a"}
    PR_SMART_PREFERRED_AUDIO_CODECS = ("mp4a", "aac")
    PR_SUPPORTED_AUDIO_CODECS = (
        "pcm",
        "sowt",
        "twos",
        "alac",
        "mp4a",
        "aac",
        "mp3",
    )
    PR_UNSUPPORTED_AUDIO_CODECS = ("opus", "vorbis")
    PR_SMART_AUDIO_FILTERS = (
        "[ext=m4a]",
        "[ext=aac]",
        "[acodec*=mp4a]",
        "[acodec*=aac]",
        "[ext=mp3]",
        "[acodec*=mp3]",
        "[ext=wav]",
        "[ext=aif]",
        "[ext=aiff]",
        "[ext=bwf]",
    )
    SIMPLE_PRESETS = [
        ("best_quality", "最高画质（自动组装）"),
        ("mp4_compatible", "MP4 兼容优先"),
        ("pr_smart", "PR智能预设"),
        ("custom_preferences", "自定义"),
        ("audio_only", "仅音频"),
        ("subtitle_only", "仅字幕"),
        ("thumbnail_only", "仅封面"),
    ]
    PROFESSIONAL_MODES = [
        ("video_audio", "音视频（可组装）"),
        ("video", "仅视频"),
        ("audio", "仅音频"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preview_thread = None
        self.download_thread = None
        self.auto_hotword_extraction_thread = None
        self.auto_hotword_extraction_target = None
        self.preview_data = None
        self.parsed_url = ""
        self.last_result = {}
        self.last_selection_summary = "暂无"
        self.latest_download_detail = {}
        self.selected_video_format = None
        self.selected_audio_format = None
        self.video_button_group = QButtonGroup(self)
        self.audio_button_group = QButtonGroup(self)
        self.video_button_group.setExclusive(True)
        self.audio_button_group.setExclusive(True)
        self.current_mode_key = "simple"
        self.time_range_rows = []
        self.controls_enabled = True
        self.download_action_state = "idle"
        self._pending_download_request = None
        self._pending_subtitle_mode = "manual"
        self._pending_work_dir = ""
        self._last_persisted_progress = -1
        self._restoring_download_preferences = True
        self._compact_layout_active = None

        self.setObjectName("DownloadCenterInterface")
        self.setWindowTitle(self.tr("下载中心"))
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setup_ui()
        self._restore_download_preferences()
        self.setup_signals()
        self._restoring_download_preferences = False
        cfg.themeMode.valueChanged.connect(lambda *_: self._apply_theme_styles())
        self._refresh_download_strategy_hint()
        self._refresh_output_dir_labels()
        self._toggle_subtitle_mode_row(self.subtitle_checkbox.isChecked())
        self._set_preview_visible(False)
        self._set_result_actions_enabled(False)
        self._reset_result_labels()
        self._reset_download_detail_panel()
        self._refresh_selection_summary()
        self._apply_theme_styles()
        self._on_professional_mode_changed()
        self._set_download_action_state("idle")
        self._restore_download_state()

    def _serializable_preview(self) -> dict | None:
        if not self.preview_data:
            return None
        preview = {
            key: value
            for key, value in self.preview_data.items()
            if key not in {"thumbnail_bytes", "info_dict"}
        }
        duration = (self.preview_data.get("info_dict") or {}).get("duration")
        preview["info_dict"] = {"duration": duration}
        thumbnail_bytes = self.preview_data.get("thumbnail_bytes")
        preview["thumbnail_base64"] = (
            base64.b64encode(thumbnail_bytes).decode("ascii") if thumbnail_bytes else ""
        )
        return preview

    def _save_download_state(
        self,
        *,
        status: str = "ready",
        request: dict | None = None,
        progress: int | None = None,
        detail: dict | None = None,
    ) -> None:
        preview = self._serializable_preview()
        if not preview:
            return
        previous = {}
        try:
            previous = json.loads(self.DOWNLOAD_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        stored_request = (
            previous.get("request")
            if request is None and status in {"downloading", "interrupted"}
            else request
        )
        payload = {
            "version": 1,
            "status": status,
            "url": self.parsed_url,
            "preview": preview,
            "request": stored_request,
            "subtitle_mode": self._pending_subtitle_mode,
            "work_dir": self._pending_work_dir or self._effective_output_dir(),
            "progress": int(self.progress_bar.value() if progress is None else progress),
            "detail": detail if detail is not None else previous.get("detail", {}),
        }
        temporary = self.DOWNLOAD_STATE_PATH.with_suffix(".tmp")
        try:
            self.DOWNLOAD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.DOWNLOAD_STATE_PATH)
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _restore_download_state(self) -> None:
        try:
            payload = json.loads(self.DOWNLOAD_STATE_PATH.read_text(encoding="utf-8"))
            preview = dict(payload["preview"])
            encoded_thumbnail = preview.pop("thumbnail_base64", "")
            preview["thumbnail_bytes"] = (
                base64.b64decode(encoded_thumbnail) if encoded_thumbnail else None
            )
            url = str(payload.get("url") or preview.get("url") or "").strip()
            if not url or not self._is_valid_url(url):
                return
        except (OSError, ValueError, TypeError, KeyError):
            return

        self.url_input.setText(url)
        self.preview_data = preview
        self.parsed_url = url
        self._set_preview_visible(True)
        self._render_preview_card(preview)
        self._populate_video_table(preview.get("video_formats") or [])
        self._populate_audio_table(preview.get("audio_formats") or [])
        self._populate_subtitle_choices()
        self._pending_download_request = payload.get("request") or None
        self._pending_subtitle_mode = str(payload.get("subtitle_mode") or "manual")
        self._pending_work_dir = str(payload.get("work_dir") or "")
        progress = max(0, min(100, int(payload.get("progress") or 0)))
        self.progress_bar.setValue(progress)
        detail = payload.get("detail") or {}
        if detail:
            self._render_download_detail_panel(detail)
        if self._pending_download_request and payload.get("status") in {"downloading", "interrupted"}:
            self.start_button.setText(self.tr("继续下载"))
            self.status_label.setText(self.tr("已恢复上次下载进度，点击继续下载"))
        else:
            self._pending_download_request = None
            self.status_label.setText(self.tr("已恢复上次解析结果"))
        self._refresh_selection_summary()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(36, 20, 36, 20)
        self.main_layout.setSpacing(12)

        self._setup_top_bar()
        self._setup_url_card()
        self._setup_content_area()
        self._setup_result_area()
        self._setup_bottom_bar()

        self._switch_download_mode("simple")
        self.mode_switch.setMinimumHeight(36)
        self.mode_panel_container.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        self.content_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._adjust_responsive_layout()

    def _setup_top_bar(self):
        top_layout = QHBoxLayout()
        top_layout.setSpacing(12)

        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.command_bar.addAction(Action(FIF.LINK, self.tr("解析链接"), triggered=self.parse_link))
        self.command_bar.addSeparator()
        self.open_folder_action = Action(FIF.FOLDER, self.tr("打开文件夹"), triggered=self.open_result_folder)
        self.command_bar.addAction(self.open_folder_action)
        self.send_to_transcription_action = Action(FIF.SEND, self.tr("送去转录"), triggered=self.send_download_to_transcription)
        self.command_bar.addAction(self.send_to_transcription_action)
        top_layout.addWidget(self.command_bar, 1)

        self.terminate_button = PushButton(self.tr("终止下载"), self, icon=FIF.CANCEL)
        self.terminate_button.setFixedHeight(34)
        self.terminate_button.setVisible(False)
        top_layout.addWidget(self.terminate_button)

        self.start_button = PrimaryPushButton(self.tr("开始下载"), self, icon=FIF.DOWNLOAD)
        self.start_button.setFixedHeight(34)
        top_layout.addWidget(self.start_button)

        self.main_layout.addLayout(top_layout)

    def _setup_url_card(self):
        self.url_card = CardWidget(self)
        url_card_layout = QHBoxLayout(self.url_card)
        url_card_layout.setContentsMargins(20, 14, 20, 14)
        url_card_layout.setSpacing(12)

        url_icon = IconWidget(FIF.LINK, self.url_card)
        url_icon.setFixedSize(20, 20)
        url_icon.setAccessibleName(self.tr("视频链接"))

        self.url_input = LineEdit(self.url_card)
        self.url_input.setPlaceholderText(self.tr("请输入视频 URL，支持 B站 / YouTube / Twitter 等平台链接"))
        self.url_input.setClearButtonEnabled(True)
        self.url_input.setMinimumHeight(36)
        self.url_input.setAccessibleName(self.tr("视频链接"))
        self.url_input.setAccessibleDescription(
            self.tr("粘贴视频链接后按回车即可解析")
        )

        self.parse_button = PushButton(self.tr("解析"), self.url_card)
        self.parse_button.setFixedWidth(80)
        self.parse_button.setAccessibleName(self.tr("解析视频链接"))

        url_card_layout.addWidget(url_icon)
        url_card_layout.addWidget(self.url_input, 1)
        url_card_layout.addWidget(self.parse_button)

        self.main_layout.addWidget(self.url_card)

    def _setup_content_area(self):
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setObjectName("downloadContentScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.content_scroll.setMinimumHeight(220)

        self.scroll_content = QWidget(self.content_scroll)
        self.scroll_content.setObjectName("downloadScrollContent")
        self.scroll_content_layout = QVBoxLayout(self.scroll_content)
        self.scroll_content_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_content_layout.setSpacing(12)

        self._setup_preview_card()
        self._setup_config_cards()

        self.scroll_content_layout.addWidget(self.preview_card)
        self.scroll_content_layout.addWidget(self.config_card)
        self.scroll_content_layout.addStretch(1)
        self.content_scroll.setWidget(self.scroll_content)

        self.main_layout.addWidget(self.content_scroll, 1)

    def _setup_preview_card(self):
        self.preview_card = CardWidget(self.scroll_content)
        self.preview_card.setObjectName("downloadPreviewPanel")
        self.preview_card_layout = QHBoxLayout(self.preview_card)
        self.preview_card_layout.setContentsMargins(20, 16, 20, 16)
        self.preview_card_layout.setSpacing(16)

        self.thumbnail_label = AspectRatioLabel(self.preview_card)
        self.thumbnail_label.setObjectName("downloadThumbnailLabel")
        self.thumbnail_label.setMinimumWidth(100)
        self.thumbnail_label.setMaximumWidth(280)
        self.thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setText(self.tr("暂无封面"))

        preview_info_layout = QVBoxLayout()
        preview_info_layout.setContentsMargins(0, 0, 0, 0)
        preview_info_layout.setSpacing(6)
        self.preview_title_label = BodyLabel(self.tr("标题：暂无"), self.preview_card)
        self.preview_title_label.setObjectName("downloadTitleLabel")
        self.preview_title_label.setWordWrap(True)
        self.preview_uploader_label = BodyLabel(self.tr("作者：暂无"), self.preview_card)
        self.preview_uploader_label.setObjectName("downloadHintLabel")
        self.preview_uploader_label.setWordWrap(True)
        self.preview_meta_label = BodyLabel(self.tr("时长 / 日期 / 播放量：暂无"), self.preview_card)
        self.preview_meta_label.setObjectName("downloadHintLabel")
        self.preview_meta_label.setWordWrap(True)
        self.preview_subtitle_label = BodyLabel(self.tr("字幕：暂无"), self.preview_card)
        self.preview_subtitle_label.setObjectName("downloadHintLabel")
        self.preview_subtitle_label.setWordWrap(True)
        preview_info_layout.addWidget(self.preview_title_label)
        preview_info_layout.addWidget(self.preview_uploader_label)
        preview_info_layout.addWidget(self.preview_meta_label)
        preview_info_layout.addWidget(self.preview_subtitle_label)
        preview_info_layout.addStretch(1)

        self.preview_card_layout.addWidget(self.thumbnail_label, 2)
        self.preview_card_layout.addLayout(preview_info_layout, 5)

    def _setup_config_cards(self):
        self.config_card = CardWidget(self.scroll_content)
        config_outer_layout = QVBoxLayout(self.config_card)
        config_outer_layout.setContentsMargins(0, 0, 0, 0)
        config_outer_layout.setSpacing(0)

        self._setup_selection_section(config_outer_layout)
        self._setup_separator(config_outer_layout)
        self._setup_options_section(config_outer_layout)

    def _setup_selection_section(self, parent_layout):
        self.selection_section = QWidget()
        selection_layout = QVBoxLayout(self.selection_section)
        selection_layout.setContentsMargins(20, 16, 20, 8)
        selection_layout.setSpacing(12)

        self.mode_switch = SegmentedWidget(self.selection_section)
        self.mode_panel_container = QWidget(self.selection_section)
        self.mode_panel_layout = QVBoxLayout(self.mode_panel_container)
        self.mode_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.mode_panel_layout.setSpacing(0)
        self.mode_switch.addItem(routeKey="simple", text=self.tr("简易模式"), onClick=lambda: self._switch_download_mode("simple"))
        self.mode_switch.addItem(routeKey="professional", text=self.tr("专业模式"), onClick=lambda: self._switch_download_mode("professional"))

        self.simple_panel = QWidget(self.selection_section)
        simple_layout = QVBoxLayout(self.simple_panel)
        simple_layout.setContentsMargins(0, 0, 0, 0)
        simple_layout.setSpacing(10)
        simple_row = QHBoxLayout()
        simple_row.setSpacing(12)
        self.simple_preset_label = BodyLabel(self.tr("下载预设"), self.simple_panel)
        self.simple_preset_label.setObjectName("downloadPrimaryLabel")
        self.simple_preset_combo = ComboBox(self.simple_panel)
        for key, text in self.SIMPLE_PRESETS:
            self.simple_preset_combo.addItem(text, userData=key)
        self.simple_preset_combo.setMinimumWidth(200)
        simple_row.addWidget(self.simple_preset_label)
        simple_row.addWidget(self.simple_preset_combo)
        simple_row.addStretch(1)
        self.simple_hint_label = BodyLabel(self.tr("简易模式会自动使用稳定的 yt-dlp 格式策略，不需要手动挑选 format_id。"), self.simple_panel)
        self.simple_hint_label.setObjectName("downloadHintLabel")
        self.simple_hint_label.setWordWrap(True)

        self.custom_preferences_section = QWidget(self.simple_panel)
        custom_preferences_layout = QVBoxLayout(self.custom_preferences_section)
        custom_preferences_layout.setContentsMargins(0, 0, 0, 0)
        custom_preferences_layout.setSpacing(10)

        custom_preferences_title = BodyLabel(self.tr("自定义偏好"), self.custom_preferences_section)
        custom_preferences_title.setObjectName("downloadPrimaryLabel")

        self.custom_video_codec_label = BodyLabel(self.tr("视频编码偏好"), self.custom_preferences_section)
        self.custom_video_codec_label.setObjectName("downloadPrimaryLabel")
        self.custom_video_codec_combo = ComboBox(self.custom_preferences_section)
        self.custom_video_codec_combo.addItem(self.tr("自动"), userData="auto")
        self.custom_video_codec_combo.addItem("AVC1", userData="avc1")
        self.custom_video_codec_combo.addItem("AV1", userData="av01")
        self.custom_video_codec_combo.addItem("VP9", userData="vp9")
        self.custom_video_codec_combo.setMinimumWidth(160)

        self.custom_container_label = BodyLabel(self.tr("容器偏好"), self.custom_preferences_section)
        self.custom_container_label.setObjectName("downloadPrimaryLabel")
        self.custom_container_combo = ComboBox(self.custom_preferences_section)
        self.custom_container_combo.addItem(self.tr("自动"), userData="auto")
        self.custom_container_combo.addItem("MP4", userData="mp4")
        self.custom_container_combo.addItem("WebM", userData="webm")
        self.custom_container_combo.setMinimumWidth(160)

        self.custom_audio_codec_label = BodyLabel(self.tr("音频偏好"), self.custom_preferences_section)
        self.custom_audio_codec_label.setObjectName("downloadPrimaryLabel")
        self.custom_audio_codec_combo = ComboBox(self.custom_preferences_section)
        self.custom_audio_codec_combo.addItem(self.tr("自动"), userData="auto")
        self.custom_audio_codec_combo.addItem("MP4A", userData="mp4a")
        self.custom_audio_codec_combo.addItem("Opus", userData="opus")
        self.custom_audio_codec_combo.setMinimumWidth(160)

        self.custom_preferences_grid = QGridLayout()
        self.custom_preferences_grid.setContentsMargins(0, 0, 0, 0)
        self.custom_preferences_grid.setHorizontalSpacing(12)
        self.custom_preferences_grid.setVerticalSpacing(8)
        self.custom_preference_fields = (
            (self.custom_video_codec_label, self.custom_video_codec_combo),
            (self.custom_container_label, self.custom_container_combo),
            (self.custom_audio_codec_label, self.custom_audio_codec_combo),
        )

        self.custom_preferences_hint_label = BodyLabel(
            self.tr("自定义预设会按你的偏好组合下载格式，并保留自动回退策略。"),
            self.custom_preferences_section,
        )
        self.custom_preferences_hint_label.setObjectName("downloadHintLabel")
        self.custom_preferences_hint_label.setWordWrap(True)

        simple_layout.addLayout(simple_row)
        simple_layout.addWidget(self.simple_hint_label)
        custom_preferences_layout.addWidget(custom_preferences_title)
        custom_preferences_layout.addLayout(self.custom_preferences_grid)
        custom_preferences_layout.addWidget(self.custom_preferences_hint_label)
        simple_layout.addWidget(self.custom_preferences_section)
        self.custom_preferences_section.setVisible(False)

        self.pr_smart_postprocess_checkbox = CheckBox(
            self.tr("若下载结果为 AV1/VP9，则额外转为 H.265"),
            self.simple_panel,
        )
        self.pr_smart_postprocess_checkbox.setVisible(False)
        simple_layout.addWidget(self.pr_smart_postprocess_checkbox)
        self.pr_smart_transcript_checkbox = CheckBox(
            self.tr("生成视频文稿（使用 YouTube 字幕）"),
            self.simple_panel,
        )
        self.pr_smart_transcript_checkbox.setVisible(False)
        simple_layout.addWidget(self.pr_smart_transcript_checkbox)

        self.professional_panel = QWidget(self.selection_section)
        professional_layout = QVBoxLayout(self.professional_panel)
        professional_layout.setContentsMargins(0, 0, 0, 0)
        professional_layout.setSpacing(10)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(12)
        self.professional_mode_label = BodyLabel(self.tr("下载模式"), self.professional_panel)
        self.professional_mode_label.setObjectName("downloadPrimaryLabel")
        self.professional_mode_combo = ComboBox(self.professional_panel)
        for key, text in self.PROFESSIONAL_MODES:
            self.professional_mode_combo.addItem(text, userData=key)
        self.professional_mode_combo.setMinimumWidth(200)
        mode_row.addWidget(self.professional_mode_label)
        mode_row.addWidget(self.professional_mode_combo)
        mode_row.addStretch(1)
        self.video_section_title = BodyLabel(self.tr("视频流"), self.professional_panel)
        self.video_section_title.setObjectName("downloadPrimaryLabel")
        self.audio_section_title = BodyLabel(self.tr("音频流"), self.professional_panel)
        self.audio_section_title.setObjectName("downloadPrimaryLabel")
        self.video_table = self._create_format_table(self.professional_panel)
        self.audio_table = self._create_format_table(self.professional_panel)
        professional_layout.addLayout(mode_row)
        self.professional_postprocess_checkbox = CheckBox(
            self.tr("若下载结果为 AV1/VP9，则额外转为 H.265"),
            self.professional_panel,
        )
        professional_layout.addWidget(self.professional_postprocess_checkbox)
        professional_layout.addWidget(self.video_section_title)
        professional_layout.addWidget(self.video_table)
        professional_layout.addWidget(self.audio_section_title)
        professional_layout.addWidget(self.audio_table)

        self.mode_panel_layout.addWidget(self.simple_panel)
        self.mode_panel_layout.addWidget(self.professional_panel)
        self.professional_panel.hide()
        self.selection_summary_label = BodyLabel(self.tr("已选方案：暂无"), self.selection_section)
        self.selection_summary_label.setObjectName("downloadPrimaryLabel")
        self.selection_summary_label.setWordWrap(True)
        self.selection_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        selection_layout.addWidget(self.mode_switch)
        selection_layout.addWidget(self.mode_panel_container)
        selection_layout.addWidget(self.selection_summary_label)

        parent_layout.addWidget(self.selection_section)

    def _setup_separator(self, parent_layout):
        separator = QFrame(self.config_card)
        separator.setFrameShape(QFrame.HLine)
        separator.setObjectName("downloadSeparator")
        separator.setFixedHeight(1)
        parent_layout.addWidget(separator)

    def _setup_options_section(self, parent_layout):
        self.options_section = QWidget()
        options_layout = QVBoxLayout(self.options_section)
        options_layout.setContentsMargins(20, 12, 20, 16)
        options_layout.setSpacing(12)

        self.options_header_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.options_header_layout.setSpacing(12)
        options_title = BodyLabel(self.tr("附加下载项"), self.options_section)
        options_title.setObjectName("downloadSectionTitle")
        self.subtitle_checkbox = CheckBox(self.tr("下载字幕"), self.options_section)
        self.thumbnail_checkbox = CheckBox(self.tr("下载封面"), self.options_section)
        self.metadata_checkbox = CheckBox(self.tr("下载元数据"), self.options_section)
        self.description_txt_checkbox = CheckBox(self.tr("生成说明TXT"), self.options_section)
        self.description_txt_checkbox.setChecked(True)
        self.thumbnail_checkbox.setChecked(True)
        self.download_option_checkboxes = (
            self.subtitle_checkbox,
            self.thumbnail_checkbox,
            self.metadata_checkbox,
            self.description_txt_checkbox,
        )
        self.download_options_container = QWidget(self.options_section)
        self.download_options_grid = QGridLayout(self.download_options_container)
        self.download_options_grid.setContentsMargins(0, 0, 0, 0)
        self.download_options_grid.setHorizontalSpacing(16)
        self.download_options_grid.setVerticalSpacing(8)
        self.options_header_layout.addWidget(options_title)
        self.options_header_layout.addWidget(self.download_options_container, 1)

        self.subtitle_mode_row = QWidget(self.options_section)
        subtitle_mode_layout = QHBoxLayout(self.subtitle_mode_row)
        subtitle_mode_layout.setContentsMargins(0, 0, 0, 0)
        subtitle_mode_layout.setSpacing(12)
        self.subtitle_mode_label = BodyLabel(self.tr("字幕来源"), self.subtitle_mode_row)
        self.subtitle_mode_label.setObjectName("downloadPrimaryLabel")
        self.subtitle_source_combo = GroupedSubtitleComboBox(self.subtitle_mode_row)
        self.subtitle_source_combo.addItem(
            self.tr("解析链接后显示可用字幕"), userData=None
        )
        self.subtitle_source_combo.setItemEnabled(0, False)
        self.subtitle_source_combo.setMinimumWidth(300)
        subtitle_mode_layout.addWidget(self.subtitle_mode_label)
        subtitle_mode_layout.addWidget(self.subtitle_source_combo)
        subtitle_mode_layout.addStretch(1)

        self.time_range_section = QWidget(self.options_section)
        time_range_layout = QVBoxLayout(self.time_range_section)
        time_range_layout.setContentsMargins(0, 0, 0, 0)
        time_range_layout.setSpacing(8)

        time_range_header = QHBoxLayout()
        time_range_header.setContentsMargins(0, 0, 0, 0)
        time_range_header.setSpacing(12)
        self.enable_time_ranges_checkbox = CheckBox(self.tr("启用时间段下载"), self.time_range_section)
        self.multi_time_ranges_checkbox = CheckBox(self.tr("多时间段"), self.time_range_section)
        self.multi_time_ranges_checkbox.setEnabled(False)
        time_range_header.addWidget(self.enable_time_ranges_checkbox)
        time_range_header.addWidget(self.multi_time_ranges_checkbox)
        time_range_header.addStretch(1)

        self.time_range_hint_label = BodyLabel(
            self.tr("支持 MM:SS 或 HH:MM:SS；时间段仅作用于主媒体下载。"),
            self.time_range_section,
        )
        self.time_range_hint_label.setObjectName("downloadHintLabel")
        self.time_range_hint_label.setWordWrap(True)

        self.time_ranges_container = QWidget(self.time_range_section)
        self.time_ranges_layout = QVBoxLayout(self.time_ranges_container)
        self.time_ranges_layout.setContentsMargins(0, 0, 0, 0)
        self.time_ranges_layout.setSpacing(8)

        self.add_time_range_button = PushButton(self.tr("添加时间段"), self.time_range_section)
        self.add_time_range_button.setMaximumWidth(120)

        time_range_layout.addLayout(time_range_header)
        time_range_layout.addWidget(self.time_range_hint_label)
        time_range_layout.addWidget(self.time_ranges_container)
        time_range_layout.addWidget(self.add_time_range_button, 0, Qt.AlignLeft)

        self._add_time_range_row()
        self._update_time_range_ui_state()

        self.output_dir_row = QWidget(self.options_section)
        self.output_dir_layout = QBoxLayout(QBoxLayout.LeftToRight, self.output_dir_row)
        self.output_dir_layout.setContentsMargins(0, 0, 0, 0)
        self.output_dir_layout.setSpacing(12)
        self.output_dir_title = BodyLabel(self.tr("输出目录"), self.output_dir_row)
        self.output_dir_title.setObjectName("downloadPrimaryLabel")
        self.output_dir_value = BodyLabel("", self.output_dir_row)
        self.output_dir_value.setObjectName("downloadPathValueLabel")
        self.output_dir_value.setWordWrap(True)
        self.choose_output_dir_button = PushButton(self.tr("选择目录"), self.output_dir_row)
        self.reset_output_dir_button = PushButton(self.tr("跟随工作目录"), self.output_dir_row)
        self.output_dir_layout.addWidget(self.output_dir_title)
        self.output_dir_layout.addWidget(self.output_dir_value, 1)
        self.output_dir_layout.addWidget(self.choose_output_dir_button)
        self.output_dir_layout.addWidget(self.reset_output_dir_button)

        self.download_checkboxes = (
            self.pr_smart_postprocess_checkbox,
            self.pr_smart_transcript_checkbox,
            self.professional_postprocess_checkbox,
            *self.download_option_checkboxes,
            self.enable_time_ranges_checkbox,
            self.multi_time_ranges_checkbox,
        )
        for checkbox in self.download_checkboxes:
            checkbox.setMinimumHeight(28)

        self.strategy_label = BodyLabel("", self.options_section)
        self.strategy_label.setObjectName("downloadHintLabel")
        self.strategy_label.setWordWrap(True)

        options_layout.addLayout(self.options_header_layout)
        options_layout.addWidget(self.subtitle_mode_row)
        options_layout.addWidget(self.time_range_section)
        options_layout.addWidget(self.output_dir_row)
        options_layout.addWidget(self.strategy_label)

        parent_layout.addWidget(self.options_section)

    def _setup_result_area(self):
        self.result_card = CardWidget(self)
        self.result_card.setObjectName("downloadResultPanel")
        result_layout = QVBoxLayout(self.result_card)
        result_layout.setContentsMargins(20, 16, 20, 16)
        result_layout.setSpacing(8)

        result_header = QHBoxLayout()
        self.result_title = BodyLabel(self.tr("下载结果"), self.result_card)
        self.result_title.setObjectName("downloadSectionTitle")
        result_header.addWidget(self.result_title)
        result_header.addStretch(1)

        self.result_summary = BodyLabel("", self.result_card)
        self.result_summary.setWordWrap(True)
        self.result_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_summary.setObjectName("downloadPathValueLabel")

        result_details = QHBoxLayout()
        result_details.setSpacing(24)
        self.result_work_dir = BodyLabel("", self.result_card)
        self.result_work_dir.setWordWrap(True)
        self.result_work_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_work_dir.setObjectName("downloadPathValueLabel")
        self.result_media = BodyLabel("", self.result_card)
        self.result_media.setWordWrap(True)
        self.result_media.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_media.setObjectName("downloadPathValueLabel")
        result_details.addWidget(self.result_work_dir, 1)
        result_details.addWidget(self.result_media, 1)

        result_files = QHBoxLayout()
        result_files.setSpacing(16)
        self.result_video = BodyLabel("", self.result_card)
        self.result_video.setWordWrap(True)
        self.result_video.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_video.setObjectName("downloadPathValueLabel")
        self.result_audio = BodyLabel("", self.result_card)
        self.result_audio.setWordWrap(True)
        self.result_audio.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_audio.setObjectName("downloadPathValueLabel")
        self.result_subtitle = BodyLabel("", self.result_card)
        self.result_subtitle.setWordWrap(True)
        self.result_subtitle.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_subtitle.setObjectName("downloadPathValueLabel")
        self.result_thumbnail = BodyLabel("", self.result_card)
        self.result_thumbnail.setWordWrap(True)
        self.result_thumbnail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_thumbnail.setObjectName("downloadPathValueLabel")
        self.result_metadata = BodyLabel("", self.result_card)
        self.result_metadata.setWordWrap(True)
        self.result_metadata.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_metadata.setObjectName("downloadPathValueLabel")
        self.result_description_txt = BodyLabel("", self.result_card)
        self.result_description_txt.setWordWrap(True)
        self.result_description_txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_description_txt.setObjectName("downloadPathValueLabel")
        self.result_transcript_txt = BodyLabel("", self.result_card)
        self.result_transcript_txt.setWordWrap(True)
        self.result_transcript_txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_transcript_txt.setObjectName("downloadPathValueLabel")
        self.result_terms_txt = BodyLabel("", self.result_card)
        self.result_terms_txt.setWordWrap(True)
        self.result_terms_txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_terms_txt.setObjectName("downloadPathValueLabel")
        self.result_transcoded = BodyLabel("", self.result_card)
        self.result_transcoded.setWordWrap(True)
        self.result_transcoded.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_transcoded.setObjectName("downloadPathValueLabel")
        result_files.addWidget(self.result_video, 1)
        result_files.addWidget(self.result_audio, 1)
        result_files.addWidget(self.result_subtitle, 1)

        result_files2 = QHBoxLayout()
        result_files2.setSpacing(16)
        result_files2.addWidget(self.result_thumbnail, 1)
        result_files2.addWidget(self.result_metadata, 1)
        result_files2.addWidget(self.result_description_txt, 1)

        result_files3 = QHBoxLayout()
        result_files3.setSpacing(16)
        result_files3.addWidget(self.result_transcript_txt, 1)
        result_files3.addWidget(self.result_terms_txt, 1)
        result_files3.addWidget(self.result_transcoded, 1)

        result_layout.addLayout(result_header)
        result_layout.addWidget(self.result_summary)
        result_layout.addLayout(result_details)
        result_layout.addLayout(result_files)
        result_layout.addLayout(result_files2)
        result_layout.addLayout(result_files3)

        self.result_card.setVisible(False)
        self.main_layout.addWidget(self.result_card)

    def _setup_bottom_bar(self):
        self.bottom_layout = QHBoxLayout()
        self.bottom_layout.setSpacing(12)
        self.progress_stack = QStackedWidget(self)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setValue(0)
        self.indeterminate_progress_bar = IndeterminateProgressBar(self, start=False)
        self.progress_stack.addWidget(self.progress_bar)
        self.progress_stack.addWidget(self.indeterminate_progress_bar)
        self.progress_stack.setCurrentWidget(self.progress_bar)
        self.status_label = BodyLabel(self.tr("等待解析链接"), self)
        self.status_label.setObjectName("downloadHintLabel")
        self.status_label.setMinimumWidth(120)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.download_detail_button = PushButton(self.tr("详情"), self)
        self.download_detail_button.setFixedHeight(30)
        self.download_detail_button.setFixedWidth(64)
        self.download_detail_panel = BodyLabel(self.tr("暂无实时下载数据"), self)
        self.download_detail_panel.setObjectName("downloadPathValueLabel")
        self.download_detail_panel.setWordWrap(True)
        self.download_detail_panel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.download_detail_panel.setVisible(False)
        self.bottom_layout.addWidget(self.progress_stack, 1)
        self.bottom_layout.addWidget(self.status_label)
        self.bottom_layout.addWidget(self.download_detail_button)
        self.main_layout.addLayout(self.bottom_layout)
        self.main_layout.addWidget(self.download_detail_panel)

    def _create_format_table(self, parent: QWidget) -> QTableWidget:
        table = QTableWidget(parent)
        table.setObjectName("downloadFormatTable")
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels([self.tr("选择"), self.tr("质量"), self.tr("详情")])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.setMinimumHeight(120)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        return table

    def setup_signals(self):
        self.url_input.textChanged.connect(self._on_url_text_changed)
        self.url_input.returnPressed.connect(self.parse_link)
        self.parse_button.clicked.connect(self.parse_link)
        self.start_button.clicked.connect(self._on_start_button_clicked)
        self.terminate_button.clicked.connect(self._on_terminate_button_clicked)
        self.subtitle_checkbox.toggled.connect(self._toggle_subtitle_mode_row)
        self.subtitle_checkbox.toggled.connect(self._refresh_selection_summary)
        self.thumbnail_checkbox.toggled.connect(self._refresh_selection_summary)
        self.metadata_checkbox.toggled.connect(self._refresh_selection_summary)
        self.description_txt_checkbox.toggled.connect(self._refresh_selection_summary)
        self.subtitle_checkbox.toggled.connect(self._save_download_preferences)
        self.thumbnail_checkbox.toggled.connect(self._save_download_preferences)
        self.metadata_checkbox.toggled.connect(self._save_download_preferences)
        self.description_txt_checkbox.toggled.connect(self._save_download_preferences)
        self.choose_output_dir_button.clicked.connect(self._choose_output_dir)
        self.reset_output_dir_button.clicked.connect(self._reset_output_dir)
        self.simple_preset_combo.currentIndexChanged.connect(self._on_simple_preset_changed)
        self.professional_mode_combo.currentIndexChanged.connect(self._on_professional_mode_changed)
        self.enable_time_ranges_checkbox.toggled.connect(self._on_enable_time_ranges_toggled)
        self.multi_time_ranges_checkbox.toggled.connect(self._on_multi_time_ranges_toggled)
        self.add_time_range_button.clicked.connect(self._on_add_time_range_clicked)
        self.custom_video_codec_combo.currentIndexChanged.connect(self._refresh_selection_summary)
        self.custom_container_combo.currentIndexChanged.connect(self._refresh_selection_summary)
        self.custom_audio_codec_combo.currentIndexChanged.connect(self._refresh_selection_summary)
        self.pr_smart_postprocess_checkbox.toggled.connect(self._on_postprocess_checkbox_toggled)
        self.professional_postprocess_checkbox.toggled.connect(self._on_postprocess_checkbox_toggled)
        self.pr_smart_transcript_checkbox.toggled.connect(self._on_pr_smart_transcript_toggled)
        self.subtitle_source_combo.currentIndexChanged.connect(
            self._on_subtitle_source_changed
        )
        self.custom_video_codec_combo.currentIndexChanged.connect(self._save_download_preferences)
        self.custom_container_combo.currentIndexChanged.connect(self._save_download_preferences)
        self.custom_audio_codec_combo.currentIndexChanged.connect(self._save_download_preferences)
        self.pr_smart_transcript_checkbox.toggled.connect(self._save_download_preferences)
        self.download_detail_button.clicked.connect(self._toggle_download_detail_panel)

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_theme_styles()
        self._refresh_download_strategy_hint()
        self._refresh_output_dir_labels()
        self._adjust_responsive_layout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_responsive_layout()

    def _apply_theme_styles(self):
        if isDarkTheme():
            page_background = "#202124"
            card_background = "rgba(255, 255, 255, 0.05)"
            card_border = "rgba(255, 255, 255, 0.08)"
            primary_color = "#F5F5F5"
            hint_color = "#999999"
            value_color = "#D4D4D4"
            title_color = "#FFFFFF"
            separator_color = "rgba(255, 255, 255, 0.08)"
            table_header_background = "rgba(255, 255, 255, 0.06)"
            table_grid = "rgba(255, 255, 255, 0.06)"
            thumbnail_bg = "rgba(255, 255, 255, 0.04)"
            thumbnail_border = "rgba(255, 255, 255, 0.08)"
        else:
            page_background = "#F5F7FA"
            card_background = "#FFFFFF"
            card_border = "rgba(17, 24, 39, 0.12)"
            primary_color = "#1D2939"
            hint_color = "#667085"
            value_color = "#475467"
            title_color = "#101828"
            separator_color = "rgba(17, 24, 39, 0.12)"
            table_header_background = "#F2F4F7"
            table_grid = "rgba(17, 24, 39, 0.10)"
            thumbnail_bg = "#F8FAFC"
            thumbnail_border = "rgba(17, 24, 39, 0.14)"

        self.setStyleSheet(f"""
            QWidget#DownloadCenterInterface {{ background: {page_background}; }}
            CardWidget {{
                background-color: {card_background};
                border: 1px solid {card_border};
                border-radius: 8px;
            }}
            QWidget#downloadScrollContent {{
                background: transparent;
            }}
            QScrollArea#downloadContentScroll {{
                background: transparent;
                border: none;
            }}
            QLabel#downloadTitleLabel {{
                color: {title_color};
                font-size: 15px;
                font-weight: 600;
            }}
            QLabel#downloadSectionTitle {{
                color: {title_color};
                font-size: 14px;
                font-weight: 600;
            }}
            QLabel#downloadPrimaryLabel {{ color: {primary_color}; }}
            QLabel#downloadHintLabel {{ color: {hint_color}; font-size: 13px; }}
            QLabel#downloadPathValueLabel {{ color: {value_color}; font-size: 13px; }}
            CheckBox, QCheckBox {{
                color: {primary_color};
                spacing: 6px;
            }}
            CheckBox:disabled, QCheckBox:disabled {{
                color: {hint_color};
            }}
            QLabel#downloadThumbnailLabel {{
                color: {hint_color};
                border: 1px solid {thumbnail_border};
                border-radius: 8px;
                background-color: {thumbnail_bg};
            }}
            QFrame#downloadSeparator {{
                background-color: {separator_color};
                border: none;
            }}
            QTableWidget#downloadFormatTable {{
                background: {card_background};
                color: {value_color};
                gridline-color: {table_grid};
                border: 1px solid {separator_color};
                border-radius: 8px;
            }}
            QHeaderView::section {{
                background: {table_header_background};
                color: {primary_color};
                border: none;
                padding: 6px;
                font-size: 13px;
            }}
        """)
        for checkbox in getattr(self, "download_checkboxes", ()):
            checkbox.setMinimumHeight(28)

    def _switch_download_mode(self, mode_key: str):
        if mode_key not in {"simple", "professional"}:
            mode_key = "simple"
        self.current_mode_key = mode_key
        self.mode_switch.setCurrentItem(mode_key)
        self.simple_panel.setVisible(mode_key == "simple")
        self.professional_panel.setVisible(mode_key == "professional")
        self._adjust_responsive_layout()
        self.mode_panel_layout.invalidate()
        self.mode_panel_container.updateGeometry()
        self.selection_section.updateGeometry()
        self._update_custom_preferences_visibility()
        self._refresh_selection_summary()
        self._save_download_preferences()

    @staticmethod
    def _set_combo_current_data(combo: ComboBox, value: str, default: str):
        target = str(value or default)
        default_index = 0
        for index in range(combo.count()):
            item_data = str(combo.itemData(index) or "")
            if item_data == default:
                default_index = index
            if item_data == target:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(default_index)

    def _restore_download_preferences(self):
        self._set_combo_current_data(
            self.simple_preset_combo,
            str(cfg.get(cfg.download_center_simple_preset) or "best_quality"),
            "best_quality",
        )
        self._set_combo_current_data(
            self.professional_mode_combo,
            str(cfg.get(cfg.download_center_professional_mode) or "video_audio"),
            "video_audio",
        )
        self._set_combo_current_data(
            self.custom_video_codec_combo,
            str(cfg.get(cfg.download_center_custom_video_codec) or "auto"),
            "auto",
        )
        self._set_combo_current_data(
            self.custom_container_combo,
            str(cfg.get(cfg.download_center_custom_container) or "auto"),
            "auto",
        )
        self._set_combo_current_data(
            self.custom_audio_codec_combo,
            str(cfg.get(cfg.download_center_custom_audio_codec) or "auto"),
            "auto",
        )

        self.subtitle_checkbox.setChecked(bool(cfg.get(cfg.download_center_need_subtitle)))
        self.thumbnail_checkbox.setChecked(bool(cfg.get(cfg.download_center_need_thumbnail)))
        self.metadata_checkbox.setChecked(bool(cfg.get(cfg.download_center_need_metadata)))
        self.description_txt_checkbox.setChecked(bool(cfg.get(cfg.download_center_need_description_txt)))
        postprocess_enabled = bool(cfg.get(cfg.download_center_pr_smart_postprocess))
        self.pr_smart_postprocess_checkbox.setChecked(postprocess_enabled)
        self.professional_postprocess_checkbox.setChecked(postprocess_enabled)
        self.pr_smart_transcript_checkbox.setChecked(bool(cfg.get(cfg.download_center_pr_smart_transcript_txt)))

        mode_key = str(cfg.get(cfg.download_center_mode) or "simple")
        self._switch_download_mode(mode_key)
        self._on_professional_mode_changed()
        self._toggle_subtitle_mode_row(self.subtitle_checkbox.isChecked())

    def _save_download_preferences(self, *_args):
        if self._restoring_download_preferences:
            return
        cfg.set(cfg.download_center_mode, self.current_mode_key)
        cfg.set(cfg.download_center_simple_preset, self.simple_preset_combo.currentData() or "best_quality")
        cfg.set(cfg.download_center_professional_mode, self.professional_mode_combo.currentData() or "video_audio")
        cfg.set(cfg.download_center_need_subtitle, self.subtitle_checkbox.isChecked())
        cfg.set(cfg.download_center_need_thumbnail, self.thumbnail_checkbox.isChecked())
        cfg.set(cfg.download_center_need_metadata, self.metadata_checkbox.isChecked())
        cfg.set(cfg.download_center_need_description_txt, self.description_txt_checkbox.isChecked())
        cfg.set(cfg.download_center_subtitle_mode, self._selected_subtitle_mode())
        cfg.set(
            cfg.download_center_subtitle_language,
            self._selected_subtitle_language(),
        )
        cfg.set(cfg.download_center_custom_video_codec, self.custom_video_codec_combo.currentData() or "auto")
        cfg.set(cfg.download_center_custom_container, self.custom_container_combo.currentData() or "auto")
        cfg.set(cfg.download_center_custom_audio_codec, self.custom_audio_codec_combo.currentData() or "auto")
        cfg.set(cfg.download_center_pr_smart_postprocess, self.pr_smart_postprocess_checkbox.isChecked())
        cfg.set(cfg.download_center_pr_smart_transcript_txt, self.pr_smart_transcript_checkbox.isChecked())

    def _set_preview_visible(self, visible: bool):
        self.preview_card.setVisible(visible)
        self.config_card.setVisible(visible)
        self.start_button.setEnabled(visible)
        self._adjust_responsive_layout()

    def _adjust_responsive_layout(self):
        if not hasattr(self, "video_table") or not hasattr(self, "audio_table"):
            return

        viewport_width = self.content_scroll.viewport().width()
        content_width = viewport_width if viewport_width > 0 else self.width()
        compact = content_width <= self.COMPACT_CONTENT_WIDTH

        if self._compact_layout_active != compact:
            self._compact_layout_active = compact
            direction = (
                QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight
            )
            self.preview_card_layout.setDirection(direction)
            self.options_header_layout.setDirection(direction)
            self.output_dir_layout.setDirection(direction)
            self._reflow_download_option_checkboxes(compact)
            self._reflow_custom_preference_fields(compact)
            self.scroll_content_layout.invalidate()
            self.scroll_content.updateGeometry()

        if compact:
            self.thumbnail_label.setMaximumWidth(320)
            self.thumbnail_label.setMaximumHeight(180)
            self.preview_card_layout.setAlignment(
                self.thumbnail_label, Qt.AlignHCenter
            )
        else:
            self.thumbnail_label.setMaximumWidth(280)
            self.thumbnail_label.setMaximumHeight(16777215)
            self.preview_card_layout.setAlignment(
                self.thumbnail_label, Qt.AlignVCenter
            )

        visible_tables = int(self.video_table.isVisible()) + int(self.audio_table.isVisible())
        if visible_tables <= 0:
            return

        height = max(self.height(), 520)
        total_budget = max(220, min(520, height - 380))
        per_table_height = max(120, min(320, total_budget // visible_tables))
        if self.video_table.isVisible():
            self.video_table.setMinimumHeight(per_table_height)
        if self.audio_table.isVisible():
            self.audio_table.setMinimumHeight(per_table_height)

    def _reflow_download_option_checkboxes(self, compact: bool):
        while self.download_options_grid.count():
            self.download_options_grid.takeAt(0)

        column_count = 2 if compact else len(self.download_option_checkboxes)
        for index, checkbox in enumerate(self.download_option_checkboxes):
            row, column = divmod(index, column_count)
            self.download_options_grid.addWidget(checkbox, row, column)

        for column in range(len(self.download_option_checkboxes)):
            self.download_options_grid.setColumnStretch(
                column, 1 if column < column_count else 0
            )

    def _reflow_custom_preference_fields(self, compact: bool):
        while self.custom_preferences_grid.count():
            self.custom_preferences_grid.takeAt(0)
        for column in range(len(self.custom_preference_fields) * 2 + 1):
            self.custom_preferences_grid.setColumnStretch(column, 0)

        if compact:
            for row, (label, combo) in enumerate(self.custom_preference_fields):
                self.custom_preferences_grid.addWidget(label, row, 0)
                self.custom_preferences_grid.addWidget(combo, row, 1)
            self.custom_preferences_grid.setColumnStretch(1, 1)
            return

        for index, (label, combo) in enumerate(self.custom_preference_fields):
            column = index * 2
            self.custom_preferences_grid.addWidget(label, 0, column)
            self.custom_preferences_grid.addWidget(combo, 0, column + 1)
        self.custom_preferences_grid.setColumnStretch(
            len(self.custom_preference_fields) * 2, 1
        )

    def _set_controls_enabled(self, enabled: bool):
        self.controls_enabled = enabled
        self.url_input.setEnabled(enabled)
        self.parse_button.setEnabled(enabled)
        self.simple_preset_combo.setEnabled(enabled)
        self.professional_mode_combo.setEnabled(enabled)
        professional_mode = self.professional_mode_combo.currentData() or "video_audio"
        professional_needs_video = professional_mode in {"video", "video_audio"}
        self.professional_postprocess_checkbox.setEnabled(enabled and professional_needs_video)
        self.subtitle_checkbox.setEnabled(enabled)
        self.thumbnail_checkbox.setEnabled(enabled)
        self.metadata_checkbox.setEnabled(enabled)
        self.description_txt_checkbox.setEnabled(enabled)
        subtitle_source_needed = self.subtitle_checkbox.isChecked() or (
            self._is_pr_smart_preset_selected() and self.pr_smart_transcript_checkbox.isChecked()
        )
        self.subtitle_source_combo.setEnabled(
            enabled and subtitle_source_needed and self._has_available_subtitle_choice()
        )
        self.choose_output_dir_button.setEnabled(enabled)
        self.reset_output_dir_button.setEnabled(enabled)
        self.custom_video_codec_combo.setEnabled(enabled and self._is_custom_simple_preset_selected())
        self.custom_container_combo.setEnabled(enabled and self._is_custom_simple_preset_selected())
        self.custom_audio_codec_combo.setEnabled(enabled and self._is_custom_simple_preset_selected())
        self._update_time_range_ui_state()
        self._update_custom_preferences_visibility()
        self._refresh_start_button_state()
        self.video_table.setEnabled(enabled)
        self.audio_table.setEnabled(enabled)

    def _refresh_start_button_state(self):
        if self.download_action_state == "downloading":
            self.terminate_button.setVisible(False)
            self.start_button.setText(self.tr("暂停下载"))
            self.start_button.setToolTip(self.tr("点击后先暂停下载"))
            self.start_button.setEnabled(True)
            return

        if self.download_action_state == "paused":
            self.terminate_button.setText(self.tr("终止下载"))
            self.terminate_button.setToolTip(self.tr("终止下载并清理当前数据"))
            self.terminate_button.setEnabled(True)
            self.terminate_button.setVisible(True)
            self.start_button.setText(self.tr("继续下载"))
            self.start_button.setToolTip(self.tr("从当前进度继续下载"))
            self.start_button.setEnabled(True)
            return

        if self.download_action_state == "terminating":
            self.terminate_button.setText(self.tr("终止中…"))
            self.terminate_button.setToolTip(self.tr("正在终止下载并清理当前数据"))
            self.terminate_button.setEnabled(False)
            self.terminate_button.setVisible(True)
            self.start_button.setText(self.tr("继续下载"))
            self.start_button.setToolTip(self.tr("下载正在终止"))
            self.start_button.setEnabled(False)
            return

        self.terminate_button.setVisible(False)
        if self._pending_download_request:
            self.start_button.setText(self.tr("继续下载"))
            self.start_button.setToolTip(self.tr("使用已保存的方案继续下载"))
            self.start_button.setEnabled(self.controls_enabled)
            return
        issue = self._download_readiness_issue()
        self.start_button.setText(issue or self.tr("开始下载"))
        self.start_button.setToolTip(issue or self.tr("按当前方案开始下载"))
        self.start_button.setEnabled(self.controls_enabled and not issue)

    def _download_readiness_issue(self):
        if not self.preview_data or self.url_input.text().strip() != self.parsed_url:
            return self.tr("请先解析链接")
        if self.current_mode_key == "professional":
            mode = self.professional_mode_combo.currentData() or "video_audio"
            if mode in {"video", "video_audio"} and not self.selected_video_format:
                return self.tr("请选择视频流")
            if mode == "audio" or (mode == "video_audio" and not self.selected_video_format.get("has_audio")):
                if not self.selected_audio_format:
                    return self.tr("请选择音频流")
        elif (self.simple_preset_combo.currentData() == "subtitle_only"
              and not self._has_available_subtitle_choice()
              and not any(box.isChecked() for box in (self.thumbnail_checkbox, self.metadata_checkbox, self.description_txt_checkbox))):
            return self.tr("当前视频没有字幕")
        return ""

    def _set_download_action_state(self, state: str):
        self.download_action_state = state
        self._refresh_start_button_state()

    def _on_start_button_clicked(self):
        if self.download_action_state == "idle":
            self.start_download()
            return

        if not self.download_thread or not self.download_thread.isRunning():
            self.download_thread = None
            self._set_download_action_state("idle")
            return

        if self.download_action_state == "downloading":
            self.download_thread.request_pause()
            self._set_download_action_state("paused")
            self.status_label.setText(self.tr("正在暂停下载…"))
            return

        if self.download_action_state == "paused":
            self.download_thread.request_resume()
            self._set_download_action_state("downloading")
            self.status_label.setText(self.tr("正在继续下载…"))

    def _on_terminate_button_clicked(self):
        if self.download_action_state != "paused":
            return
        if not self.download_thread or not self.download_thread.isRunning():
            self.download_thread = None
            self._set_download_action_state("idle")
            return
        self.download_thread.request_terminate()
        self._set_download_action_state("terminating")
        self.status_label.setText(self.tr("正在终止下载并清理当前数据…"))

    def _set_result_actions_enabled(self, enabled: bool, has_video: bool = False):
        self.open_folder_action.setEnabled(enabled)
        self.send_to_transcription_action.setEnabled(enabled and has_video)

    def _toggle_download_detail_panel(self):
        visible = not self.download_detail_panel.isVisible()
        self.download_detail_panel.setVisible(visible)
        self.download_detail_button.setText(self.tr("收起") if visible else self.tr("详情"))
        self._adjust_responsive_layout()

    def _reset_download_detail_panel(self):
        self.latest_download_detail = {}
        self.download_detail_panel.setText(self.tr("暂无实时下载数据"))
        self.download_detail_panel.setVisible(False)
        self.download_detail_button.setText(self.tr("详情"))
        self._set_progress_indeterminate(False)

    def _set_progress_indeterminate(self, enabled: bool):
        if not hasattr(self, "progress_stack"):
            return
        if enabled:
            self.progress_stack.setCurrentWidget(self.indeterminate_progress_bar)
            if not self.indeterminate_progress_bar.isStarted():
                self.indeterminate_progress_bar.start()
            return
        self.indeterminate_progress_bar.stop()
        self.progress_stack.setCurrentWidget(self.progress_bar)

    def _render_download_detail_panel(self, detail: dict):
        self.latest_download_detail = detail or {}
        if not self.latest_download_detail:
            self.download_detail_panel.setText(self.tr("暂无实时下载数据"))
            return

        parts = []
        phase = str(self.latest_download_detail.get("phase") or "").strip()
        status = str(self.latest_download_detail.get("status") or "").strip()
        section_index = self.latest_download_detail.get("section_index")
        section_count = self.latest_download_detail.get("section_count")
        indeterminate = bool(self.latest_download_detail.get("indeterminate"))
        self._set_progress_indeterminate(indeterminate)
        if status:
            self.status_label.setText(status)
            phase_text = status
            if section_index and section_count:
                phase_text += self.tr("（片段 {0}/{1}）").format(section_index, section_count)
            parts.append(self.tr("阶段：") + phase_text)

        filename = str(self.latest_download_detail.get("filename") or "").strip()
        if filename:
            parts.append(self.tr("文件：") + filename)

        amount = str(self.latest_download_detail.get("downloaded") or "").strip()
        total = str(self.latest_download_detail.get("total") or "").strip()
        if amount or total:
            progress_text = amount
            if total:
                progress_text = f"{amount or '?'} / {total}"
            amount_label = self.tr("已生成：") if phase == "processing" else self.tr("已下载：")
            parts.append(amount_label + progress_text)

        percent = str(self.latest_download_detail.get("percent") or "").strip()
        speed = str(self.latest_download_detail.get("speed") or "").strip()
        eta = str(self.latest_download_detail.get("eta") or "").strip()
        elapsed = str(self.latest_download_detail.get("elapsed") or "").strip()
        if percent:
            parts.append(self.tr("进度：") + percent + "%")
            if phase:
                try:
                    self.progress_bar.setValue(max(0, min(100, int(float(percent)))))
                except ValueError:
                    pass
        if speed:
            parts.append(self.tr("速度：") + speed)
        if eta:
            parts.append(self.tr("剩余：") + eta)
        if elapsed:
            parts.append(self.tr("已用时：") + elapsed)

        self.download_detail_panel.setText("\n".join(parts) if parts else self.tr("暂无实时下载数据"))

    def _reset_result_labels(self):
        self.result_summary.setText(self.tr("方案摘要：暂无"))
        self.result_work_dir.setText(self.tr("输出目录：暂无"))
        self.result_media.setText(self.tr("主媒体：暂无"))
        self.result_video.setText(self.tr("视频：暂无"))
        self.result_audio.setText(self.tr("音频：暂无"))
        self.result_subtitle.setText(self.tr("字幕：暂无"))
        self.result_thumbnail.setText(self.tr("封面：暂无"))
        self.result_metadata.setText(self.tr("元数据：暂无"))
        self.result_description_txt.setText(self.tr("说明TXT：暂无"))
        self.result_transcript_txt.setText(self.tr("视频文稿：暂无"))
        self.result_terms_txt.setText(self.tr("AI术语表：请在 WhisperX 热词管理中手动生成"))
        self.result_transcoded.setText(self.tr("H.265后处理：暂无"))

    def _set_terms_result_text(self, message: str):
        if hasattr(self, "result_terms_txt"):
            self.result_terms_txt.setText(self.tr("AI术语表：") + str(message))

    @staticmethod
    def _current_transcribe_model_value() -> str:
        current_model = cfg.transcribe_model.value
        return str(getattr(current_model, "value", current_model) or "")

    def _auto_hotword_target(self) -> tuple[str, str]:
        if self._current_transcribe_model_value() == TranscribeModelEnum.MLX_WHISPER.value:
            return "mlx", "MLX Whisper"
        return "whisperx", "WhisperX"

    def _clear_auto_hotword_prompt(self, target_key: str):
        from app.core.utils.transcript_terms import remove_generated_document_prompt_terms

        if target_key == "mlx":
            cfg.set(cfg.mlx_hotwords, "")
        else:
            cfg.set(cfg.whisperx_hotwords, "")
        cfg.set(
            cfg.custom_prompt_text,
            remove_generated_document_prompt_terms(cfg.custom_prompt_text.value),
        )

    def _apply_auto_hotword_terms(self, terms: list):
        from app.core.utils.transcript_terms import (
            apply_terms_to_mlx_hotwords,
            apply_terms_to_whisperx_hotwords,
        )

        if self.auto_hotword_extraction_target == "mlx":
            return apply_terms_to_mlx_hotwords(terms)
        return apply_terms_to_whisperx_hotwords(terms)

    def _target_language(self) -> str:
        value = cfg.target_language.value
        return str(getattr(value, "value", value) or "")

    def _maybe_start_auto_hotword_extraction(self, result: dict):
        transcript_path = str(result.get("transcript_txt_path") or "").strip()
        if not transcript_path or not Path(transcript_path).is_file():
            return
        self._start_auto_hotword_extraction(transcript_path)

    def _start_auto_hotword_extraction(self, transcript_path: str):
        if self.auto_hotword_extraction_thread is not None:
            message = self.tr("已有热词提取任务正在进行，已跳过本次自动提取")
            self.last_result["terms_message"] = message
            self._set_terms_result_text(message)
            return

        from app.components.WhisperXSettingWidget import HotwordExtractionThread
        from app.core.subtitle_processor.prompt import (
            PROMPT_TERM_GLOSSARY,
            get_prompt_template,
        )

        target_key, _ = self._auto_hotword_target()
        self.auto_hotword_extraction_target = target_key
        self._clear_auto_hotword_prompt(target_key)

        message = self.tr("正在从下载生成的视频文稿提取热词...")
        self.last_result["terms_txt_path"] = None
        self.last_result["terms_message"] = message
        self._set_terms_result_text(message)
        if hasattr(self, "status_label"):
            self.status_label.setText(message)

        self.auto_hotword_extraction_thread = HotwordExtractionThread(
            transcript_path,
            get_prompt_template(PROMPT_TERM_GLOSSARY),
            self._target_language(),
            self,
        )
        self.auto_hotword_extraction_thread.status_changed.connect(
            self._update_auto_hotword_extraction_status
        )
        self.auto_hotword_extraction_thread.succeeded.connect(
            self._on_auto_hotwords_extracted
        )
        self.auto_hotword_extraction_thread.failed.connect(
            self._on_auto_hotword_extraction_failed
        )
        self.auto_hotword_extraction_thread.finished.connect(
            self._on_auto_hotword_extraction_finished
        )
        self.auto_hotword_extraction_thread.start()

    def _update_auto_hotword_extraction_status(self, status: str):
        message = self.tr("正在从下载生成的视频文稿提取热词：") + self.tr(status)
        self.last_result["terms_message"] = message
        self._set_terms_result_text(message)
        if hasattr(self, "status_label"):
            self.status_label.setText(message)

    def _on_auto_hotwords_extracted(self, terms: list):
        try:
            if not terms:
                message = self.tr("未提取到可用热词")
                self.last_result["terms_message"] = message
                self._set_terms_result_text(message)
                if hasattr(self, "status_label"):
                    self.status_label.setText(self.tr("下载完成，未提取到可用热词"))
                InfoBar.warning(
                    self.tr("未提取到热词"),
                    self.tr("AI 未返回可用名称或术语，当前热词已保持为空。"),
                    duration=3500,
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                )
                return

            self._apply_auto_hotword_terms(terms)
            from app.core.utils.transcript_terms import write_terms_txt_file

            transcript_path = Path(str(self.last_result.get("transcript_txt_path") or ""))
            filename_stem = transcript_path.stem.replace("【视频文稿】", "") or "视频文稿"
            work_dir = Path(self.last_result.get("work_dir") or transcript_path.parent)
            terms_txt_path = write_terms_txt_file(terms, work_dir, filename_stem)
            message = self.tr("已自动提取并覆盖写入 {0} 条热词").format(len(terms))
            self.last_result["terms_txt_path"] = terms_txt_path
            self.last_result["terms_message"] = message
            self._set_terms_result_text(terms_txt_path)
            if hasattr(self, "status_label"):
                self.status_label.setText(self.tr("下载完成，热词提取完成"))
            InfoBar.success(
                self.tr("热词提取完成"),
                message,
                duration=3500,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
            )
        except Exception as exc:
            self._on_auto_hotword_extraction_failed(str(exc))

    def _on_auto_hotword_extraction_failed(self, error: str):
        message = self.tr("自动提取失败：") + str(error)
        self.last_result["terms_txt_path"] = None
        self.last_result["terms_message"] = message
        self._set_terms_result_text(message)
        if hasattr(self, "status_label"):
            self.status_label.setText(self.tr("下载完成，热词自动提取失败"))
        InfoBar.error(
            self.tr("热词提取失败"),
            message,
            duration=5000,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
        )

    def _on_auto_hotword_extraction_finished(self):
        if self.auto_hotword_extraction_thread is not None:
            if hasattr(self.auto_hotword_extraction_thread, "deleteLater"):
                self.auto_hotword_extraction_thread.deleteLater()
            self.auto_hotword_extraction_thread = None
        self.auto_hotword_extraction_target = None

    def _reset_preview_labels(self):
        self.preview_title_label.setText(self.tr("标题：暂无"))
        self.preview_uploader_label.setText(self.tr("作者：暂无"))
        self.preview_meta_label.setText(self.tr("时长 / 日期 / 播放量：暂无"))
        self.preview_subtitle_label.setText(self.tr("字幕：暂无"))
        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText(self.tr("暂无封面"))

    def _clear_preview(self):
        self.preview_data = None
        self.parsed_url = ""
        self.selected_video_format = None
        self.selected_audio_format = None
        self._reset_preview_labels()
        self._clear_table(self.video_table)
        self._clear_table(self.audio_table)
        self._set_preview_visible(False)
        self._reset_subtitle_choices()
        self._refresh_selection_summary()
        self._pending_download_request = None
        try:
            self.DOWNLOAD_STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_table(self, table: QTableWidget):
        table.setRowCount(0)
        table.clearContents()

    def _on_url_text_changed(self, *_args):
        current_url = self.url_input.text().strip()
        if self.preview_data and current_url != self.parsed_url:
            self.status_label.setText(self.tr("链接已变更，请重新解析"))
            self._clear_preview()

    def _toggle_subtitle_mode_row(self, checked: bool | None = None):
        subtitle_source_needed = self.subtitle_checkbox.isChecked() or (
            self._is_pr_smart_preset_selected() and self.pr_smart_transcript_checkbox.isChecked()
        )
        self.subtitle_mode_row.setVisible(subtitle_source_needed)
        self.subtitle_source_combo.setEnabled(
            self.controls_enabled
            and subtitle_source_needed
            and self._has_available_subtitle_choice()
        )
        self._refresh_selection_summary()

    def _on_subtitle_source_changed(self, *_args):
        self._save_download_preferences()
        self._refresh_selection_summary()

    @staticmethod
    def _unique_subtitle_languages(languages) -> list[str]:
        candidates = []
        for language in languages or []:
            language = str(language).strip()
            if language and language not in candidates:
                candidates.append(language)
        return candidates

    @staticmethod
    def _subtitle_language_label_text(language: str) -> str:
        if language.lower() == "en" or language.lower().startswith("en-"):
            return f"English ({language})"
        return language

    def _reset_subtitle_choices(self, message: str | None = None) -> None:
        if not hasattr(self, "subtitle_source_combo"):
            return
        blocked = self.subtitle_source_combo.blockSignals(True)
        self.subtitle_source_combo.clear()
        self.subtitle_source_combo.set_separator_before([])
        self.subtitle_source_combo.addItem(
            message or self.tr("解析链接后显示可用字幕"), userData=None
        )
        self.subtitle_source_combo.setItemEnabled(0, False)
        self.subtitle_source_combo.blockSignals(blocked)
        self.subtitle_source_combo.setEnabled(False)

    def _populate_subtitle_choices(self):
        if not hasattr(self, "subtitle_source_combo"):
            return
        manual_languages = self._unique_subtitle_languages(
            (self.preview_data or {}).get("manual_subtitle_languages")
        )
        auto_languages = self._unique_subtitle_languages(
            (self.preview_data or {}).get("auto_subtitle_languages")
        )
        if not manual_languages and not auto_languages:
            self._reset_subtitle_choices(self.tr("未检测到可用字幕"))
            return

        preferred_mode = str(
            cfg.get(cfg.download_center_subtitle_mode) or "manual"
        ).lower()
        preferred = str(cfg.get(cfg.download_center_subtitle_language) or "en").lower()
        previous = self.subtitle_source_combo.currentData()
        blocked = self.subtitle_source_combo.blockSignals(True)
        self.subtitle_source_combo.clear()

        choices = []
        for mode, source_label, languages in (
            ("manual", self.tr("人工字幕"), manual_languages),
            ("auto", self.tr("自动字幕"), auto_languages),
        ):
            for language in languages:
                choice = (mode, language)
                choices.append(choice)
                self.subtitle_source_combo.addItem(
                    f"{source_label} · {self._subtitle_language_label_text(language)}",
                    userData=choice,
                )

        separator_indices = (
            [len(manual_languages)] if manual_languages and auto_languages else []
        )
        self.subtitle_source_combo.set_separator_before(separator_indices)

        target = previous if previous in choices else None
        if target is None:
            target = next(
                (
                    choice
                    for wanted_mode, wanted_language in (
                        (preferred_mode, preferred),
                        (preferred_mode, "en"),
                        ("manual", preferred),
                        ("manual", "en"),
                        ("auto", preferred),
                        ("auto", "en"),
                    )
                    for choice in choices
                    if choice[0] == wanted_mode
                    and (
                        choice[1].lower() == wanted_language
                        or choice[1].lower().startswith(wanted_language + "-")
                    )
                ),
                choices[0],
            )
        self.subtitle_source_combo.setCurrentIndex(choices.index(target))
        self.subtitle_source_combo.blockSignals(blocked)
        self._toggle_subtitle_mode_row()

    def _has_available_subtitle_choice(self) -> bool:
        data = self.subtitle_source_combo.currentData()
        return isinstance(data, tuple) and len(data) == 2

    def _is_custom_simple_preset_selected(self) -> bool:
        return (self.simple_preset_combo.currentData() or "") == "custom_preferences"

    def _is_pr_smart_preset_selected(self) -> bool:
        return (self.simple_preset_combo.currentData() or "") in {"pr_smart", "pr_editing"}

    def _update_custom_preferences_visibility(self):
        is_custom = self._is_custom_simple_preset_selected()
        self.custom_preferences_section.setVisible(is_custom)
        self.custom_video_codec_combo.setEnabled(self.controls_enabled and is_custom)
        self.custom_container_combo.setEnabled(self.controls_enabled and is_custom)
        self.custom_audio_codec_combo.setEnabled(self.controls_enabled and is_custom)
        is_pr_smart = self._is_pr_smart_preset_selected()
        self.pr_smart_postprocess_checkbox.setVisible(is_pr_smart)
        self.pr_smart_postprocess_checkbox.setEnabled(self.controls_enabled and is_pr_smart)
        self.pr_smart_transcript_checkbox.setVisible(is_pr_smart)
        self.pr_smart_transcript_checkbox.setEnabled(self.controls_enabled and is_pr_smart)
        self._toggle_subtitle_mode_row()

    def _on_simple_preset_changed(self, *_args):
        self._update_custom_preferences_visibility()
        self._refresh_selection_summary()
        self._save_download_preferences()

    def _on_pr_smart_transcript_toggled(self, *_args):
        self._toggle_subtitle_mode_row()
        self._refresh_selection_summary()

    def _on_postprocess_checkbox_toggled(self, checked: bool):
        sender = self.sender()
        target = (
            self.professional_postprocess_checkbox
            if sender is self.pr_smart_postprocess_checkbox
            else self.pr_smart_postprocess_checkbox
        )
        previous = target.blockSignals(True)
        target.setChecked(checked)
        target.blockSignals(previous)
        self._refresh_selection_summary()
        self._save_download_preferences()

    def _create_time_range_row(self, start_text: str = "", end_text: str = "") -> dict:
        row_widget = QWidget(self.time_ranges_container)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        start_input = LineEdit(row_widget)
        start_input.setPlaceholderText(self.tr("开始时间，如 01:30"))
        start_input.setText(start_text)
        end_input = LineEdit(row_widget)
        end_input.setPlaceholderText(self.tr("结束时间，如 03:10"))
        end_input.setText(end_text)
        delete_button = PushButton(self.tr("删除"), row_widget)
        delete_button.setMaximumWidth(72)

        row_layout.addWidget(BodyLabel(self.tr("开始"), row_widget))
        row_layout.addWidget(start_input, 1)
        row_layout.addWidget(BodyLabel(self.tr("结束"), row_widget))
        row_layout.addWidget(end_input, 1)
        row_layout.addWidget(delete_button)

        row = {
            "widget": row_widget,
            "start_input": start_input,
            "end_input": end_input,
            "delete_button": delete_button,
        }
        start_input.textChanged.connect(self._refresh_selection_summary)
        end_input.textChanged.connect(self._refresh_selection_summary)
        delete_button.clicked.connect(lambda: self._remove_time_range_row(row))
        return row

    def _add_time_range_row(self, start_text: str = "", end_text: str = ""):
        row = self._create_time_range_row(start_text, end_text)
        self.time_range_rows.append(row)
        self.time_ranges_layout.addWidget(row["widget"])
        self._update_time_range_ui_state()

    def _remove_time_range_row(self, row: dict):
        if len(self.time_range_rows) <= 1:
            row["start_input"].clear()
            row["end_input"].clear()
            self._refresh_selection_summary()
            return

        self.time_range_rows.remove(row)
        row["widget"].setParent(None)
        row["widget"].deleteLater()
        self._update_time_range_ui_state()
        self._refresh_selection_summary()

    def _visible_time_range_rows(self) -> list[dict]:
        if not self.time_range_rows:
            return []
        if self.multi_time_ranges_checkbox.isChecked():
            return self.time_range_rows
        return self.time_range_rows[:1]

    def _update_time_range_ui_state(self):
        enabled = self.enable_time_ranges_checkbox.isChecked()
        multi_enabled = enabled and self.multi_time_ranges_checkbox.isChecked()
        effective_enabled = self.controls_enabled and enabled
        self.enable_time_ranges_checkbox.setEnabled(self.controls_enabled)
        self.multi_time_ranges_checkbox.setEnabled(self.controls_enabled and enabled)
        self.time_range_hint_label.setVisible(enabled)
        self.time_ranges_container.setVisible(enabled)
        self.add_time_range_button.setVisible(multi_enabled)
        self.add_time_range_button.setEnabled(self.controls_enabled and multi_enabled)

        visible_rows = self._visible_time_range_rows()
        visible_count = len(visible_rows)
        for index, row in enumerate(self.time_range_rows):
            row_visible = enabled and (multi_enabled or index == 0)
            row["widget"].setVisible(row_visible)
            row["delete_button"].setVisible(multi_enabled and row_visible and visible_count > 1)
            row["start_input"].setEnabled(effective_enabled and row_visible)
            row["end_input"].setEnabled(effective_enabled and row_visible)
            row["delete_button"].setEnabled(self.controls_enabled and multi_enabled and row_visible and visible_count > 1)

    def _on_enable_time_ranges_toggled(self, _checked: bool):
        self._update_time_range_ui_state()
        self._refresh_selection_summary()

    def _on_multi_time_ranges_toggled(self, _checked: bool):
        self._update_time_range_ui_state()
        self._refresh_selection_summary()

    def _on_add_time_range_clicked(self):
        self._add_time_range_row()
        self._refresh_selection_summary()

    @staticmethod
    def _normalize_time_value(value: str) -> tuple[str, int] | None:
        text = str(value or "").strip()
        if not text:
            return None
        parts = text.split(":")
        if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
            return None

        try:
            if len(parts) == 2:
                minutes, seconds = map(int, parts)
                hours = 0
            else:
                hours, minutes, seconds = map(int, parts)
        except ValueError:
            return None

        if minutes < 0 or seconds < 0 or seconds >= 60 or (len(parts) == 3 and minutes >= 60):
            return None

        total_seconds = hours * 3600 + minutes * 60 + seconds
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}", total_seconds

    def _preview_duration_seconds(self) -> int | None:
        if not self.preview_data:
            return None
        try:
            duration = self.preview_data.get("info_dict", {}).get("duration")
            if duration in (None, ""):
                return None
            return int(float(duration))
        except (TypeError, ValueError):
            return None

    def _time_range_summary_text(self) -> str:
        if not self.enable_time_ranges_checkbox.isChecked():
            return ""

        visible_rows = self._visible_time_range_rows()
        if not visible_rows:
            return self.tr("片段：待填写")

        normalized_ranges = []
        for row in visible_rows:
            start_data = self._normalize_time_value(row["start_input"].text())
            end_data = self._normalize_time_value(row["end_input"].text())
            if not start_data or not end_data:
                return self.tr("片段：待填写")
            normalized_ranges.append((start_data[0], end_data[0]))

        if len(normalized_ranges) == 1:
            start_text, end_text = normalized_ranges[0]
            return self.tr("片段：") + f"{start_text} - {end_text}"
        return self.tr("片段：") + self.tr(f"{len(normalized_ranges)} 段")

    def _collect_download_sections(self, need_video: bool) -> list[str] | None:
        if not self.enable_time_ranges_checkbox.isChecked():
            return []
        if not need_video:
            InfoBar.warning(self.tr("提示"), self.tr("时间段下载仅适用于主媒体下载。"), duration=3000, parent=self)
            return None

        total_duration = self._preview_duration_seconds()
        sections = []
        for index, row in enumerate(self._visible_time_range_rows(), start=1):
            start_text = row["start_input"].text().strip()
            end_text = row["end_input"].text().strip()
            if not start_text or not end_text:
                InfoBar.warning(self.tr("提示"), self.tr(f"请完整填写第 {index} 个时间段。"), duration=3000, parent=self)
                return None
            start_data = self._normalize_time_value(start_text)
            end_data = self._normalize_time_value(end_text)
            if not start_data or not end_data:
                InfoBar.warning(self.tr("提示"), self.tr(f"第 {index} 个时间段格式无效，请使用 MM:SS 或 HH:MM:SS。"), duration=3500, parent=self)
                return None
            if start_data[1] >= end_data[1]:
                InfoBar.warning(self.tr("提示"), self.tr(f"第 {index} 个时间段的开始时间必须早于结束时间。"), duration=3500, parent=self)
                return None
            if total_duration is not None and end_data[1] > total_duration:
                InfoBar.warning(self.tr("提示"), self.tr(f"第 {index} 个时间段超过了视频总时长。"), duration=3500, parent=self)
                return None
            sections.append(f"*{start_data[0]}-{end_data[0]}")
        return sections

    def _effective_output_dir(self) -> str:
        custom_dir = str(cfg.get(cfg.download_center_output_dir) or "").strip()
        return custom_dir or str(cfg.get(cfg.work_dir))

    def _refresh_output_dir_labels(self):
        self.output_dir_value.setText(self._effective_output_dir())

    def _refresh_download_strategy_hint(self):
        strategy = str(cfg.get(cfg.download_engine_strategy) or "智能选择")
        auto_cookie = bool(cfg.get(cfg.download_auto_extract_cookies_on_startup))
        auto_cookie_text = (
            self.tr("启动时自动提取浏览器 Cookie：开启")
            if auto_cookie
            else self.tr("启动时自动提取浏览器 Cookie：关闭")
        )
        cookie_browser = str(cfg.get(cfg.download_cookie_browser) or "Safari")
        self.strategy_label.setText(
            self.tr("当前下载策略：")
            + strategy
            + "    "
            + auto_cookie_text
            + "    "
            + self.tr("Cookie 来源：")
            + cookie_browser
        )

    def _choose_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, self.tr("选择下载输出目录"), self._effective_output_dir())
        if not folder:
            return
        cfg.set(cfg.download_center_output_dir, folder)
        self._refresh_output_dir_labels()

    def _reset_output_dir(self):
        cfg.set(cfg.download_center_output_dir, "")
        self._refresh_output_dir_labels()

    @staticmethod
    def _is_valid_url(url: str) -> bool:
        try:
            parsed = urlparse(url.strip())
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except ValueError:
            return False

    def _selected_subtitle_mode(self) -> str:
        combo = self.__dict__.get("subtitle_source_combo")
        data = combo.currentData() if combo is not None else None
        if isinstance(data, tuple) and len(data) == 2:
            return str(data[0])
        return str(cfg.get(cfg.download_center_subtitle_mode) or "manual")

    def _selected_subtitle_language(self) -> str:
        combo = self.__dict__.get("subtitle_source_combo")
        data = combo.currentData() if combo is not None else None
        if isinstance(data, tuple) and len(data) == 2:
            return str(data[1])
        return str(cfg.get(cfg.download_center_subtitle_language) or "en")

    def parse_link(self):
        url = self.url_input.text().strip()
        if not self._is_valid_url(url):
            InfoBar.error(self.tr("错误"), self.tr("请输入有效的视频 URL"), duration=3000, parent=self)
            return
        cookiefile_path = APP_DATA_PATH / "cookies.txt"
        if not cookiefile_path.exists():
            InfoBar.warning(self.tr("提示"), self.tr("建议配置 cookies.txt，以提高高清视频与字幕的可用性。"), duration=4000, parent=self, position=InfoBarPosition.BOTTOM_RIGHT)
        self._set_controls_enabled(False)
        self._set_preview_visible(False)
        self.progress_bar.setValue(0)
        self.status_label.setText(self.tr("正在解析链接…"))
        try:
            from app.thread.video_download_thread import VideoPreviewThread

            self.preview_thread = VideoPreviewThread(
                url=url,
                download_engine_strategy=str(
                    cfg.get(cfg.download_engine_strategy) or "智能选择"
                ),
            )
            self.preview_thread.finished.connect(self.on_preview_finished)
            self.preview_thread.error.connect(self.on_preview_error)
            self.preview_thread.start()
        except Exception as exc:
            self.preview_thread = None
            self.on_preview_error(str(exc))

    def on_preview_finished(self, preview: dict):
        self.preview_data = preview
        self.parsed_url = preview.get("url", self.url_input.text().strip())
        self._set_controls_enabled(True)
        self._set_preview_visible(True)
        self.progress_bar.setValue(0)
        self.status_label.setText(self.tr("解析完成，请选择下载方案"))
        self._render_preview_card(preview)
        self._populate_video_table(preview.get("video_formats") or [])
        self._populate_audio_table(preview.get("audio_formats") or [])
        self._populate_subtitle_choices()
        self._adjust_responsive_layout()
        self._refresh_selection_summary()
        self._pending_download_request = None
        self._save_download_state(status="ready", progress=0)
        InfoBar.success(self.tr("解析完成"), self.tr("已获取可选格式，请确认下载方案。"), duration=2500, parent=self)

    def on_preview_error(self, error: str):
        self._set_controls_enabled(True)
        self._set_preview_visible(False)
        self.preview_data = None
        self.status_label.setText(self.tr("解析失败"))
        InfoBar.error(self.tr("解析失败"), error, duration=5000, parent=self)

    def _render_preview_card(self, preview: dict):
        self.preview_title_label.setText(self.tr("标题：") + str(preview.get("title") or "暂无"))
        self.preview_uploader_label.setText(self.tr("作者：") + str(preview.get("uploader") or "暂无"))
        meta_parts = [preview.get("duration_text") or "未知时长"]
        if preview.get("upload_date"):
            meta_parts.append(str(preview["upload_date"]))
        if preview.get("view_count_text"):
            meta_parts.append(str(preview["view_count_text"]) + self.tr(" 次观看"))
        self.preview_meta_label.setText(self.tr("时长 / 日期 / 播放量：") + " · ".join(meta_parts))
        manual = preview.get("manual_subtitle_languages") or []
        auto = preview.get("auto_subtitle_languages") or []
        subtitle_text = self.tr("字幕：") + self.tr("人工 ") + self._summarize_languages(manual)
        subtitle_text += self.tr("；自动 ") + self._summarize_languages(auto)
        self.preview_subtitle_label.setText(subtitle_text)
        self.preview_subtitle_label.setToolTip(
            self.tr("人工字幕：")
            + (", ".join(manual) if manual else self.tr("无"))
            + "\n"
            + self.tr("自动字幕：")
            + (", ".join(auto) if auto else self.tr("无"))
        )
        self.thumbnail_label.setPixmap(QPixmap())
        thumbnail_bytes = preview.get("thumbnail_bytes")
        if thumbnail_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(thumbnail_bytes)
            self.thumbnail_label.setPixmap(pixmap)
            self.thumbnail_label.setText("")
        else:
            self.thumbnail_label.setText(self.tr("暂无封面"))

    def _summarize_languages(self, languages, limit: int = 5) -> str:
        unique_languages = []
        for language in languages:
            language = str(language).strip()
            if language and language not in unique_languages:
                unique_languages.append(language)
        if not unique_languages:
            return self.tr("无")

        visible = ", ".join(unique_languages[:limit])
        if len(unique_languages) <= limit:
            return self.tr("{0} 种（{1}）").format(len(unique_languages), visible)
        return self.tr("{0} 种（{1}…）").format(len(unique_languages), visible)

    def _populate_video_table(self, formats: list[dict]):
        self.video_button_group = QButtonGroup(self)
        self.video_button_group.setExclusive(True)
        self.selected_video_format = None
        self._populate_format_table(self.video_table, formats, "video", self.video_button_group)

    def _populate_audio_table(self, formats: list[dict]):
        self.audio_button_group = QButtonGroup(self)
        self.audio_button_group.setExclusive(True)
        self.selected_audio_format = None
        self._populate_format_table(self.audio_table, formats, "audio", self.audio_button_group)

    def _populate_format_table(self, table: QTableWidget, formats: list[dict], kind: str, button_group: QButtonGroup):
        table.clearContents()
        try:
            table.cellClicked.disconnect()
        except TypeError:
            pass
        if not formats:
            table.setRowCount(1)
            table.setItem(0, 0, QTableWidgetItem("-"))
            table.setItem(0, 1, QTableWidgetItem(self.tr("暂无可选格式")))
            table.setItem(0, 2, QTableWidgetItem(self.tr("当前资源未提供对应流")))
            return

        table.setRowCount(len(formats))
        for row, fmt in enumerate(formats):
            radio = QRadioButton(table)
            radio.toggled.connect(lambda checked, data=fmt, stream_kind=kind: self._on_format_selected(stream_kind, data, checked))
            button_group.addButton(radio, row)
            radio_container = QWidget(table)
            radio_layout = QHBoxLayout(radio_container)
            radio_layout.setContentsMargins(0, 0, 0, 0)
            radio_layout.addStretch(1)
            radio_layout.addWidget(radio)
            radio_layout.addStretch(1)
            table.setCellWidget(row, 0, radio_container)
            table.setItem(row, 1, QTableWidgetItem(fmt.get("quality") or self.tr("未知")))
            table.setItem(row, 2, QTableWidgetItem(fmt.get("details") or self.tr("暂无详情")))
        table.cellClicked.connect(lambda row, _column, group=button_group: self._activate_row_radio(group, row))
        table.resizeRowsToContents()

    @staticmethod
    def _activate_row_radio(button_group: QButtonGroup, row: int):
        button = button_group.button(row)
        if button:
            button.setChecked(True)

    def _on_format_selected(self, kind: str, fmt: dict, checked: bool):
        if not checked:
            return
        if kind == "video":
            self.selected_video_format = fmt
        else:
            self.selected_audio_format = fmt
        self._refresh_selection_summary()

    def _on_professional_mode_changed(self, *_args):
        mode = self.professional_mode_combo.currentData() or "video_audio"
        needs_video = mode in {"video", "video_audio"}
        needs_audio = mode in {"audio", "video_audio"}
        self.video_section_title.setVisible(needs_video)
        self.video_table.setVisible(needs_video)
        self.video_table.setEnabled(needs_video)
        self.audio_table.setEnabled(needs_audio)
        self.audio_section_title.setVisible(needs_audio)
        self.audio_table.setVisible(needs_audio)
        self.professional_postprocess_checkbox.setVisible(needs_video)
        self.professional_postprocess_checkbox.setEnabled(self.controls_enabled and needs_video)
        self._adjust_responsive_layout()
        self._refresh_selection_summary()
        self._save_download_preferences()

    def _describe_stream(self, stream: dict | None) -> str:
        if not stream:
            return self.tr("未选择")
        quality = stream.get("quality") or self.tr("未知")
        details = stream.get("details") or ""
        return f"{quality} · {details}" if details else str(quality)

    def _combine_selector_chains(self, selectors: list[str]) -> str:
        unique_selectors = []
        seen = set()
        for selector in selectors:
            normalized = str(selector or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_selectors.append(normalized)
        return "/".join(unique_selectors)

    def _build_pr_smart_audio_pair_selector(self, video_expr: str) -> str:
        return self._combine_selector_chains(
            [f"{video_expr}+bestaudio{audio_filter}" for audio_filter in self.PR_SMART_AUDIO_FILTERS]
        )

    @staticmethod
    def _codec_rank(codec: str, preferred_codecs: tuple[str, ...]) -> int:
        lowered = str(codec or "").lower()
        for index, item in enumerate(preferred_codecs):
            if item in lowered:
                return index
        return len(preferred_codecs)

    @classmethod
    def _is_pr_supported_audio_format(cls, item: dict | None) -> bool:
        if not item:
            return False
        lowered_codec = str(item.get("acodec") or "").lower()
        lowered_ext = str(item.get("ext") or "").lower()
        if not lowered_codec or lowered_codec == "none":
            return False
        if any(codec in lowered_codec for codec in cls.PR_UNSUPPORTED_AUDIO_CODECS):
            return False
        if any(codec in lowered_codec for codec in cls.PR_SUPPORTED_AUDIO_CODECS):
            return True
        return lowered_ext in cls.PR_SUPPORTED_AUDIO_EXTS

    @classmethod
    def _is_pr_smart_preferred_audio_format(cls, item: dict | None) -> bool:
        if not cls._is_pr_supported_audio_format(item):
            return False

        lowered_codec = str(item.get("acodec") or "").lower()
        lowered_ext = str(item.get("ext") or "").lower()
        return lowered_ext in cls.PR_SMART_PREFERRED_AUDIO_EXTS or any(
            codec in lowered_codec for codec in cls.PR_SMART_PREFERRED_AUDIO_CODECS
        )

    @classmethod
    def _pr_audio_quality_key(cls, item: dict) -> tuple[int, int, int, int]:
        return (
            int(item.get("abr") or 0),
            int(item.get("filesize") or 0),
            int(item.get("channels") or 0),
            -cls._codec_rank(item.get("acodec"), cls.PR_SUPPORTED_AUDIO_CODECS),
        )

    def _pick_pr_smart_video_format(self, can_pair_supported_audio: bool | None = None) -> dict | None:
        formats = list((self.preview_data or {}).get("video_formats") or [])
        if not formats:
            return None

        if can_pair_supported_audio is None:
            can_pair_supported_audio = bool(self._pick_pr_smart_audio_format())

        compatible_formats = []
        for item in formats:
            if item.get("has_audio"):
                if self._is_pr_supported_audio_format(item):
                    compatible_formats.append(item)
            elif can_pair_supported_audio:
                compatible_formats.append(item)

        ranked_formats = compatible_formats or formats
        max_height = max(int(item.get("height") or 0) for item in ranked_formats)
        same_tier = [item for item in formats if int(item.get("height") or 0) == max_height] or formats
        preferred_codecs = (
            "hevc",
            "h265",
            "hvc1",
            "hev1",
            "avc1",
            "h264",
            "avc",
            "av01",
            "av1",
            "vp9",
        )

        def audio_rank(item: dict) -> int:
            if item.get("has_audio"):
                return 1 if self._is_pr_supported_audio_format(item) else 2
            return 0 if can_pair_supported_audio else 1

        same_tier.sort(
            key=lambda item: (
                audio_rank(item),
                self._codec_rank(item.get("vcodec"), preferred_codecs),
                -(int(item.get("fps") or 0)),
                -(int(item.get("filesize") or 0)),
            )
        )
        return same_tier[0]

    @staticmethod
    def _is_seek_friendly_avc_format(item: dict | None) -> bool:
        if not item:
            return False
        codec = str(item.get("vcodec") or "").lower()
        extension = str(item.get("ext") or "").lower()
        return extension == "mp4" and any(name in codec for name in ("avc1", "h264", "avc"))

    def _pick_automatic_time_range_baseline(
        self,
        preset: str,
        can_pair_supported_audio: bool,
    ) -> dict | None:
        if preset in {"pr_smart", "pr_editing"}:
            return self._pick_pr_smart_video_format(can_pair_supported_audio)

        formats = list((self.preview_data or {}).get("video_formats") or [])
        compatible = [
            item
            for item in formats
            if (not item.get("has_audio") and can_pair_supported_audio)
            or (item.get("has_audio") and self._is_pr_supported_audio_format(item))
        ]
        if preset == "mp4_compatible":
            compatible = [
                item for item in compatible if str(item.get("ext") or "").lower() == "mp4"
            ]
        if not compatible:
            return None

        max_height = max(int(item.get("height") or 0) for item in compatible)
        same_height = [item for item in compatible if int(item.get("height") or 0) == max_height]
        same_height.sort(
            key=lambda item: (
                int(item.get("fps") or 0),
                int(item.get("tbr") or item.get("filesize") or 0),
            ),
            reverse=True,
        )
        return same_height[0]

    def _pick_same_height_avc_format(
        self,
        baseline: dict | None,
        can_pair_supported_audio: bool,
    ) -> dict | None:
        if not baseline:
            return None
        height = int(baseline.get("height") or 0)
        candidates = []
        for item in (self.preview_data or {}).get("video_formats") or []:
            if int(item.get("height") or 0) != height:
                continue
            if not self._is_seek_friendly_avc_format(item):
                continue
            if item.get("has_audio"):
                if not self._is_pr_supported_audio_format(item):
                    continue
            elif not can_pair_supported_audio:
                continue
            candidates.append(item)

        candidates.sort(
            key=lambda item: (
                int(item.get("fps") or 0),
                int(item.get("tbr") or item.get("filesize") or 0),
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _apply_automatic_time_range_format(self, request: dict, preset: str):
        if preset not in {"best_quality", "mp4_compatible", "pr_smart", "pr_editing"}:
            return

        audio_format = self._pick_pr_smart_audio_format()
        baseline = self._pick_automatic_time_range_baseline(preset, bool(audio_format))
        avc_format = self._pick_same_height_avc_format(baseline, bool(audio_format))
        if not baseline or not avc_format:
            request["time_range_format_notice"] = self.tr(
                "片段优化：当前分辨率没有可快速定位的 MP4/AVC 流，将保留原格式，定位可能较慢"
            )
            return

        video_id = str(avc_format.get("format_id") or "")
        request.update(
            selected_video_format_id=video_id,
            selected_audio_format_id="",
            format_selector=video_id,
        )
        if not avc_format.get("has_audio") and audio_format:
            audio_id = str(audio_format.get("format_id") or "")
            request.update(
                selected_audio_format_id=audio_id,
                format_selector=f"{video_id}+{audio_id}",
            )

        quality = str(avc_format.get("quality") or f"{int(avc_format.get('height') or 0)}p")
        request["time_range_format_notice"] = self.tr(
            "片段优化：已切换为同分辨率 MP4/AVC 快速定位流"
        )
        if preset in {"pr_smart", "pr_editing"}:
            request["pr_smart_video_summary"] = f"{quality} / AVC1"

    def _pick_pr_smart_audio_format(self) -> dict | None:
        formats = list((self.preview_data or {}).get("audio_formats") or [])
        if not formats:
            return None

        ranked = [
            item for item in formats if self._is_pr_smart_preferred_audio_format(item)
        ]
        if not ranked:
            ranked = [item for item in formats if self._is_pr_supported_audio_format(item)]
        ranked.sort(key=self._pr_audio_quality_key, reverse=True)
        return ranked[0] if ranked else None

    def _build_pr_smart_request(self) -> dict:
        audio_format = self._pick_pr_smart_audio_format()
        video_format = self._pick_pr_smart_video_format(can_pair_supported_audio=bool(audio_format))
        if not video_format:
            return {
                "need_video": True,
                "download_mode": "video_audio",
                "selected_video_format_id": "",
                "selected_audio_format_id": "",
                "format_selector": self._combine_selector_chains(
                    [
                        self._build_pr_smart_audio_pair_selector("bv*"),
                        "best[ext=mp4]",
                    ]
                ),
                "pr_smart_video_summary": self.tr("回退到通用最高画质"),
                "pr_smart_audio_summary": self.tr("回退到 PR 支持音频优先"),
                "ensure_mp4_output": True,
            }

        video_id = str(video_format.get("format_id") or "")
        video_codec = str(video_format.get("vcodec") or "").upper() or self.tr("未知编码")
        video_quality = str(video_format.get("quality") or self.tr("未知画质"))
        request = {
            "need_video": True,
            "download_mode": "video_audio",
            "selected_video_format_id": video_id,
            "selected_audio_format_id": "",
            "format_selector": video_id,
            "pr_smart_video_summary": f"{video_quality} / {video_codec}",
            "pr_smart_audio_summary": self.tr("使用视频内嵌音频"),
            "ensure_mp4_output": True,
        }

        if video_format.get("has_audio"):
            if not self._is_pr_supported_audio_format(video_format):
                request["pr_smart_audio_summary"] = self.tr("未找到 PR 支持的独立音频，使用视频内嵌音频")
                return request
            return request

        if audio_format:
            audio_id = str(audio_format.get("format_id") or "")
            audio_codec = str(audio_format.get("acodec") or "").upper() or self.tr("未知编码")
            audio_quality = str(audio_format.get("quality") or self.tr("未知音频"))
            request.update(
                selected_audio_format_id=audio_id,
                format_selector=f"{video_id}+{audio_id}",
                pr_smart_audio_summary=f"{audio_quality} / {audio_codec}",
            )
            return request

        request.update(
            format_selector=self._combine_selector_chains(
                [
                    self._build_pr_smart_audio_pair_selector(video_id),
                    "bv*+bestaudio[ext=m4a]",
                    "bv*+bestaudio[ext=aac]",
                    video_id,
                ]
            ),
            pr_smart_audio_summary=self.tr("未在解析结果中找到 PR 支持音频，回退到 PR 支持音频优先"),
        )
        return request

    def _custom_preference_summary_text(self) -> str:
        codec_text = self.custom_video_codec_combo.currentText() or self.tr("自动")
        container_text = self.custom_container_combo.currentText() or self.tr("自动")
        audio_text = self.custom_audio_codec_combo.currentText() or self.tr("自动")
        return self.tr("自定义偏好：") + f"{codec_text} / {container_text} / {audio_text}"

    def _build_custom_preferences_selector(self) -> str:
        video_codec = self.custom_video_codec_combo.currentData() or "auto"
        container = self.custom_container_combo.currentData() or "auto"
        audio_codec = self.custom_audio_codec_combo.currentData() or "auto"

        video_filters = []
        if video_codec != "auto":
            video_filters.append(f"[vcodec*={video_codec}]")
        if container != "auto":
            video_filters.append(f"[ext={container}]")

        audio_filters = []
        if audio_codec != "auto":
            audio_filters.append(f"[acodec*={audio_codec}]")
        if container == "mp4":
            audio_filters.append("[ext=m4a]")
        elif container == "webm":
            audio_filters.append("[ext=webm]")

        video_expr = "bestvideo" + "".join(video_filters)
        audio_expr = "bestaudio" + "".join(audio_filters)

        selectors = [f"{video_expr}+{audio_expr}"]
        if audio_codec != "auto":
            selectors.append(f"bestvideo{''.join(video_filters)}+bestaudio[acodec*={audio_codec}]")
        if video_codec != "auto":
            selectors.append(f"bestvideo[vcodec*={video_codec}]+bestaudio")
        if container == "mp4":
            selectors.append("bestvideo[ext=mp4]+bestaudio[ext=m4a]")
        elif container == "webm":
            selectors.append("bestvideo[ext=webm]+bestaudio[ext=webm]")
        selectors.append("bv*+ba/bestvideo+bestaudio/best")
        return self._combine_selector_chains(selectors)

    def _refresh_selection_summary(self, *_args):
        parts = []
        if self.current_mode_key == "simple":
            parts.append(self.simple_preset_combo.currentText() or self.tr("暂无"))
            if self._is_custom_simple_preset_selected():
                parts.append(self._custom_preference_summary_text())
            elif self._is_pr_smart_preset_selected():
                pr_request = self._build_pr_smart_request() if self.preview_data else {}
                video_summary = pr_request.get("pr_smart_video_summary")
                audio_summary = pr_request.get("pr_smart_audio_summary")
                if video_summary:
                    parts.append(self.tr("视频策略：") + str(video_summary))
                if audio_summary:
                    parts.append(self.tr("音频策略：") + str(audio_summary))
                if self.pr_smart_postprocess_checkbox.isChecked():
                    parts.append(self.tr("AV1/VP9->H.265 后处理：开启"))
                if self.pr_smart_transcript_checkbox.isChecked():
                    parts.append(self.tr("视频文稿：开启"))
        else:
            mode_text = self.professional_mode_combo.currentText() or self.tr("暂无模式")
            parts.append(mode_text)
            current_mode = self.professional_mode_combo.currentData() or "video_audio"
            if current_mode in {"video", "video_audio"}:
                parts.append(self.tr("视频流：") + self._describe_stream(self.selected_video_format))
            if current_mode in {"audio", "video_audio"}:
                parts.append(self.tr("音频流：") + self._describe_stream(self.selected_audio_format))
            if current_mode in {"video", "video_audio"} and self.professional_postprocess_checkbox.isChecked():
                parts.append(self.tr("AV1/VP9->H.265 后处理：开启"))

        extras = []
        if self.subtitle_checkbox.isChecked():
            extras.append(
                self.tr("字幕")
                + f" ({self._selected_subtitle_language()})"
            )
        if self.thumbnail_checkbox.isChecked():
            extras.append(self.tr("封面"))
        if self.metadata_checkbox.isChecked():
            extras.append(self.tr("元数据"))
        if self.description_txt_checkbox.isChecked():
            extras.append(self.tr("说明TXT"))
        if self._is_pr_smart_preset_selected() and self.pr_smart_transcript_checkbox.isChecked():
            extras.append(self.tr("视频文稿"))
        if extras:
            parts.append(self.tr("附加项：") + self.tr("、").join(extras))

        time_range_summary = self._time_range_summary_text()
        if time_range_summary:
            parts.append(time_range_summary)

        self.last_selection_summary = self.tr("；").join(parts) if parts else self.tr("暂无")
        self.selection_summary_label.setText(self.tr("已选方案：") + self.last_selection_summary)
        self.selection_summary_label.setToolTip(self.selection_summary_label.text())
        self._refresh_start_button_state()
        self.mode_panel_layout.invalidate()
        self.mode_panel_container.updateGeometry()

    def _build_download_request(self) -> dict | None:
        request = {
            "need_video": False,
            "need_subtitle": self.subtitle_checkbox.isChecked(),
            "need_thumbnail": self.thumbnail_checkbox.isChecked(),
            "need_metadata": self.metadata_checkbox.isChecked(),
            "need_description_txt": self.description_txt_checkbox.isChecked(),
            "need_transcript_txt": False,
            "download_mode": "video_audio",
            "selected_video_format_id": "",
            "selected_audio_format_id": "",
            "format_selector": "",
            "enable_time_ranges": False,
            "multi_time_ranges": False,
            "download_sections": [],
            "pr_smart_transcode_hevc_on_av1": False,
            "ensure_mp4_output": False,
            "subtitle_language": self._selected_subtitle_language(),
        }

        simple_preset = None
        if self.current_mode_key == "simple":
            preset = self.simple_preset_combo.currentData() or "best_quality"
            simple_preset = preset
            if preset == "best_quality":
                request.update(need_video=True, download_mode="video_audio", format_selector="bv*+ba/bestvideo+bestaudio/best")
            elif preset == "mp4_compatible":
                request.update(need_video=True, download_mode="video_audio", format_selector="bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")
            elif preset in {"pr_smart", "pr_editing"}:
                request.update(self._build_pr_smart_request())
                request["pr_smart_transcode_hevc_on_av1"] = self.pr_smart_postprocess_checkbox.isChecked()
                request["need_transcript_txt"] = self.pr_smart_transcript_checkbox.isChecked()
                if request["need_transcript_txt"]:
                    request["need_subtitle"] = True
            elif preset == "custom_preferences":
                request.update(need_video=True, download_mode="video_audio", format_selector=self._build_custom_preferences_selector())
            elif preset == "audio_only":
                request.update(need_video=True, download_mode="audio", format_selector="bestaudio/best")
            elif preset == "subtitle_only":
                request["need_subtitle"] = True
            elif preset == "thumbnail_only":
                request["need_thumbnail"] = True
        else:
            mode = self.professional_mode_combo.currentData() or "video_audio"
            request["download_mode"] = mode
            if mode == "audio":
                if not self.selected_audio_format:
                    InfoBar.warning(self.tr("提示"), self.tr("请先选择一个音频流。"), duration=3000, parent=self)
                    return None
                audio_id = self.selected_audio_format.get("format_id") or ""
                request.update(need_video=True, selected_audio_format_id=audio_id, format_selector=audio_id)
            elif mode == "video":
                if not self.selected_video_format:
                    InfoBar.warning(self.tr("提示"), self.tr("请先选择一个视频流。"), duration=3000, parent=self)
                    return None
                video_id = self.selected_video_format.get("format_id") or ""
                request.update(need_video=True, selected_video_format_id=video_id, format_selector=video_id)
            else:
                if not self.selected_video_format:
                    InfoBar.warning(self.tr("提示"), self.tr("请先选择一个视频流。"), duration=3000, parent=self)
                    return None
                video_id = self.selected_video_format.get("format_id") or ""
                if self.selected_video_format.get("has_audio"):
                    request.update(need_video=True, selected_video_format_id=video_id, format_selector=video_id)
                else:
                    if not self.selected_audio_format:
                        InfoBar.warning(self.tr("提示"), self.tr("当前视频流不含音频，请再选择一个音频流。"), duration=3000, parent=self)
                        return None
                    audio_id = self.selected_audio_format.get("format_id") or ""
                    request.update(need_video=True, selected_video_format_id=video_id, selected_audio_format_id=audio_id, format_selector=f"{video_id}+{audio_id}")
            if mode in {"video", "video_audio"}:
                request["pr_smart_transcode_hevc_on_av1"] = self.professional_postprocess_checkbox.isChecked()

        if not any([request["need_video"], request["need_subtitle"], request["need_thumbnail"], request["need_metadata"]]):
            InfoBar.warning(self.tr("提示"), self.tr("请至少选择一项下载内容。"), duration=3000, parent=self)
            return None

        if request["need_subtitle"]:
            subtitle_mode = self._selected_subtitle_mode()
            has_subtitles = self.preview_data.get("has_manual_subtitles") if subtitle_mode == "manual" else self.preview_data.get("has_auto_subtitles")
            if not has_subtitles:
                InfoBar.warning(self.tr("提示"), self.tr("当前资源没有所选字幕来源，将继续执行其余下载项。"), duration=3500, parent=self)

        if self.enable_time_ranges_checkbox.isChecked():
            download_sections = self._collect_download_sections(request["need_video"])
            if download_sections is None:
                return None
            request.update(
                enable_time_ranges=True,
                multi_time_ranges=len(download_sections) > 1,
                download_sections=download_sections,
            )
            if simple_preset:
                self._apply_automatic_time_range_format(request, simple_preset)
        return request

    def start_download(self):
        url = self.url_input.text().strip()
        if not self.preview_data or url != self.parsed_url:
            InfoBar.warning(self.tr("提示"), self.tr("请先解析当前链接，再开始下载。"), duration=3000, parent=self)
            return
        request = self._pending_download_request or self._build_download_request()
        if not request:
            return
        self._refresh_selection_summary()
        time_range_notice = str(request.get("time_range_format_notice") or "").strip()
        if time_range_notice:
            self.last_selection_summary = self.last_selection_summary + self.tr("；") + time_range_notice
            self.selection_summary_label.setText(self.tr("已选方案：") + self.last_selection_summary)
        resuming = self._pending_download_request is not None
        subtitle_mode = self._pending_subtitle_mode if resuming else self._selected_subtitle_mode()
        work_dir = self._pending_work_dir if resuming and self._pending_work_dir else self._effective_output_dir()
        self._pending_download_request = dict(request)
        self._pending_subtitle_mode = subtitle_mode
        self._pending_work_dir = work_dir
        self._last_persisted_progress = -1
        self.last_result = {}
        self._reset_result_labels()
        self._reset_download_detail_panel()
        self._set_result_actions_enabled(False)
        self._set_controls_enabled(False)
        self._set_download_action_state("downloading")
        if not resuming:
            self.progress_bar.setValue(0)
        self.status_label.setText(self.tr("正在继续上次下载…") if resuming else self.tr("开始下载…"))
        self._save_download_state(
            status="downloading",
            request=self._pending_download_request,
            progress=self.progress_bar.value(),
        )
        from app.thread.video_download_thread import VideoDownloadThread

        self.download_thread = VideoDownloadThread(
            url=url,
            work_dir=work_dir,
            need_video=request["need_video"],
            need_subtitle=request["need_subtitle"],
            need_thumbnail=request["need_thumbnail"],
            subtitle_mode=subtitle_mode,
            subtitle_language=request.get("subtitle_language") or "en",
            download_engine_strategy=str(cfg.get(cfg.download_engine_strategy) or "智能选择"),
            download_mode=request["download_mode"],
            selected_video_format_id=request["selected_video_format_id"],
            selected_audio_format_id=request["selected_audio_format_id"],
            format_selector=request["format_selector"],
            need_metadata=request["need_metadata"],
            need_description_txt=request["need_description_txt"],
            description_txt_template=str(
                cfg.get(cfg.download_description_txt_template) or ""
            ),
            need_transcript_txt=request["need_transcript_txt"],
            enable_time_ranges=request["enable_time_ranges"],
            download_sections=request["download_sections"],
            pr_smart_transcode_hevc_on_av1=request["pr_smart_transcode_hevc_on_av1"],
            ensure_mp4_output=request["ensure_mp4_output"],
            resume_existing=resuming,
        )
        self.download_thread.progress.connect(self.on_download_progress)
        self.download_thread.progress_detail.connect(self.on_download_progress_detail)
        self.download_thread.detailed_finished.connect(self.on_download_finished)
        self.download_thread.cancelled.connect(self.on_download_cancelled)
        self.download_thread.error.connect(self.on_download_error)
        self.download_thread.start()

    def on_download_progress(self, value: int, status: str):
        self._set_progress_indeterminate(False)
        self.progress_bar.setValue(value)
        self.status_label.setText(status)
        if value != self._last_persisted_progress:
            self._last_persisted_progress = value
            self._save_download_state(status="downloading", progress=value)

    def on_download_progress_detail(self, detail: dict):
        self._render_download_detail_panel(detail)
        self.latest_download_detail = dict(detail)

    def on_download_finished(self, result: dict):
        self.last_result = result
        self._set_controls_enabled(True)
        self._set_download_action_state("idle")
        self.download_thread = None
        self._set_progress_indeterminate(False)
        self.progress_bar.setValue(100)
        self._pending_download_request = None
        self._save_download_state(status="ready", request=None, progress=100, detail={})
        self.status_label.setText(self.tr("下载完成"))
        self.result_summary.setText(self.tr("方案摘要：") + self.last_selection_summary)
        self.result_work_dir.setText(self.tr("输出目录：") + str(result.get("work_dir") or self.tr("暂无")))
        media_paths = result.get("media_paths") or []
        if len(media_paths) > 1:
            self.result_media.setText(self.tr("主媒体：") + self.tr(f"已下载 {len(media_paths)} 个片段文件，位于 ") + str(result.get("work_dir") or self.tr("暂无")))
        else:
            self.result_media.setText(self.tr("主媒体：") + str(result.get("media_path") or self.tr("未下载")))
        self.result_video.setText(self.tr("视频：") + str(result.get("video_path") or self.tr("未下载或不存在")))
        self.result_audio.setText(self.tr("音频：") + str(result.get("audio_path") or self.tr("未下载或不存在")))
        self.result_subtitle.setText(self.tr("字幕：") + str(result.get("subtitle_path") or self.tr("未下载或不存在")))
        self.result_thumbnail.setText(self.tr("封面：") + str(result.get("thumbnail_path") or self.tr("未下载或不存在")))
        self.result_metadata.setText(self.tr("元数据：") + str(result.get("metadata_path") or self.tr("未下载或不存在")))
        self.result_description_txt.setText(self.tr("说明TXT：") + str(result.get("description_txt_path") or self.tr("未生成或不存在")))
        self.result_transcript_txt.setText(
            self.tr("视频文稿：")
            + str(result.get("transcript_txt_path") or result.get("transcript_message") or self.tr("未生成或不存在"))
        )
        self.result_terms_txt.setText(
            self.tr("AI术语表：")
            + str(result.get("terms_txt_path") or result.get("terms_message") or self.tr("未生成或不存在"))
        )
        transcoded_path = result.get("transcoded_video_path")
        transcoded_codec = result.get("transcoded_video_codec") or self.tr("未触发")
        original_video_path = result.get("original_video_path") or self.tr("未下载或不存在")
        if transcoded_path:
            self.result_transcoded.setText(
                self.tr("H.265后处理：")
                + str(transcoded_path)
                + self.tr("（编码器：")
                + str(transcoded_codec)
                + "）"
            )
            self.result_video.setText(self.tr("视频：") + str(original_video_path))
            self.result_media.setText(self.tr("主媒体：") + str(result.get("media_path") or transcoded_path))
        else:
            postprocess_message = result.get("postprocess_message") or self.tr("未触发")
            self.result_transcoded.setText(self.tr("H.265后处理：") + str(postprocess_message))
        self.result_card.setVisible(True)
        self._adjust_responsive_layout()
        self._set_result_actions_enabled(True, has_video=bool(result.get("video_path")))
        self._maybe_start_auto_hotword_extraction(result)
        InfoBar.success(self.tr("下载完成"), self.tr("资源已下载完成。"), duration=2500, parent=self)
        send_desktop_notification(
            self.tr("下载完成"),
            self.tr("资源已下载完成。"),
            target="download_center",
        )

    def on_download_cancelled(self, message: str):
        self.last_result = {}
        self._reset_result_labels()
        self.result_card.setVisible(False)
        self._adjust_responsive_layout()
        self._set_controls_enabled(True)
        self._set_download_action_state("idle")
        self.download_thread = None
        self._set_result_actions_enabled(False)
        self.progress_bar.setValue(0)
        self._pending_download_request = None
        self._save_download_state(status="ready", request=None, progress=0, detail={})
        self.status_label.setText(self.tr("下载已终止"))
        self._reset_download_detail_panel()
        InfoBar.warning(self.tr("下载已终止"), message, duration=4000, parent=self)

    def on_download_error(self, error: str):
        self._set_controls_enabled(True)
        self._set_download_action_state("idle")
        self.download_thread = None
        self._set_result_actions_enabled(bool(self.last_result), bool(self.last_result.get("video_path")))
        self.status_label.setText(self.tr("下载失败"))
        self._save_download_state(
            status="interrupted",
            request=self._pending_download_request,
            progress=self.progress_bar.value(),
            detail=self.latest_download_detail,
        )
        self.start_button.setText(self.tr("继续下载"))
        self._reset_download_detail_panel()
        InfoBar.error(self.tr("下载失败"), error, duration=5000, parent=self)
        send_desktop_notification(
            self.tr("下载失败"),
            str(error),
            target="download_center",
        )

    def open_result_folder(self):
        target_dir = self.last_result.get("work_dir")
        if not target_dir or not Path(target_dir).exists():
            InfoBar.warning(self.tr("提示"), self.tr("当前没有可打开的下载目录。"), duration=3000, parent=self)
            return
        open_path(target_dir)

    def send_download_to_transcription(self):
        video_path = self.last_result.get("video_path")
        if not video_path:
            InfoBar.warning(self.tr("提示"), self.tr("当前结果中没有视频文件，无法送去转录。"), duration=3000, parent=self)
            return
        self.send_to_transcription.emit(video_path)
        InfoBar.success(self.tr("已发送"), self.tr("已将视频发送到语音转录页面。"), duration=2500, parent=self)
