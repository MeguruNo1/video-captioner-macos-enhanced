# -*- coding: utf-8 -*-

import datetime
import os
import sys
from dataclasses import asdict
from pathlib import Path

from PyQt5.QtCore import *
from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CardWidget,
    CommandBar,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PillPushButton,
    PrimaryPushButton,
    ProgressRing,
    RoundMenu,
    SingleDirectionScrollArea,
    TransparentDropDownPushButton,
    isDarkTheme,
    setFont,
)

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.components.LanguageSettingDialog import LanguageSettingDialog
from app.components.transcription_setting_card import TranscriptionSettingCard
from app.config import APP_DATA_PATH, RESOURCE_PATH, WORK_PATH
from app.core.entities import (
    SupportedAudioFormats,
    SupportedVideoFormats,
    TranscribeModelEnum,
    TranscribeTask,
    VideoInfo,
)
from app.core.task_factory import TaskFactory
from app.core.storage.page_state import file_signature, read_page_state, write_page_state
from app.core.utils.desktop_notification import send_desktop_notification

DEFAULT_THUMBNAIL_PATH = RESOURCE_PATH / "assets" / "default_thumbnail.jpg"


class TrackpadFriendlyScrollArea(SingleDirectionScrollArea):
    def wheelEvent(self, event):
        pixel_delta = event.pixelDelta()
        if pixel_delta.y():
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - pixel_delta.y())
            event.accept()
            return
        super().wheelEvent(event)


class VideoInfoCard(CardWidget):
    finished = pyqtSignal(TranscribeTask)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.setup_signals()
        self.task = None
        self.video_info = None
        self.transcription_interface = parent

    def setup_ui(self):
        self.setFixedHeight(150)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(20, 15, 20, 15)
        self.layout.setSpacing(20)

        self.setup_thumbnail()
        self.setup_info_layout()
        self.setup_button_layout()

    def setup_thumbnail(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        default_thumbnail_path = os.path.join(DEFAULT_THUMBNAIL_PATH)

        self.video_thumbnail = QLabel(self)
        self.video_thumbnail.setFixedSize(208, 117)
        self.video_thumbnail.setStyleSheet("background-color: #1E1F22;")
        self.video_thumbnail.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap(default_thumbnail_path).scaled(
            self.video_thumbnail.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.video_thumbnail.setPixmap(pixmap)
        self.layout.addWidget(self.video_thumbnail, 0, Qt.AlignLeft)

    def setup_info_layout(self):
        self.info_layout = QVBoxLayout()
        self.info_layout.setContentsMargins(3, 8, 3, 8)
        self.info_layout.setSpacing(10)

        self.video_title = BodyLabel(self.tr("请拖入音频或视频文件"), self)
        self.video_title.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
        self.video_title.setWordWrap(True)
        self.info_layout.addWidget(self.video_title, alignment=Qt.AlignTop)

        self.details_layout = QHBoxLayout()
        self.details_layout.setSpacing(15)

        self.resolution_info = self.create_pill_button(self.tr("画质"), 110)
        self.file_size_info = self.create_pill_button(self.tr("文件大小"), 110)
        self.duration_info = self.create_pill_button(self.tr("时长"), 100)

        self.progress_ring = ProgressRing(self)
        self.progress_ring.setFixedSize(20, 20)
        self.progress_ring.setStrokeWidth(4)
        self.progress_ring.hide()

        self.details_layout.addWidget(self.resolution_info)
        self.details_layout.addWidget(self.file_size_info)
        self.details_layout.addWidget(self.duration_info)
        self.details_layout.addWidget(self.progress_ring)
        self.details_layout.addStretch(1)
        self.info_layout.addLayout(self.details_layout)
        self.layout.addLayout(self.info_layout)

    def create_pill_button(self, text, width):
        button = PillPushButton(text, self)
        button.setCheckable(False)
        setFont(button, 11)
        # button.setFixedWidth(width)
        button.setMinimumWidth(50)
        return button

    def setup_button_layout(self):
        self.button_layout = QVBoxLayout()
        self.start_button = PrimaryPushButton(self.tr("开始转录"), self)
        self.button_layout.addWidget(self.start_button)

        self.start_button.setDisabled(True)

        button_widget = QWidget()
        button_widget.setLayout(self.button_layout)
        button_widget.setFixedWidth(130)
        self.layout.addWidget(button_widget)

    def update_info(self, video_info: VideoInfo):
        """更新视频信息显示"""
        # self.reset_ui()
        self.video_info = video_info

        self.video_title.setText(video_info.file_name.rsplit(".", 1)[0])
        self.resolution_info.setText(
            self.tr("画质: ") + f"{video_info.width}x{video_info.height}"
        )
        file_size_mb = os.path.getsize(video_info.file_path) / 1024 / 1024
        self.file_size_info.setText(self.tr("大小: ") + f"{file_size_mb:.1f} MB")
        duration = datetime.timedelta(seconds=int(video_info.duration_seconds))
        self.duration_info.setText(self.tr("时长: ") + f"{duration}")
        if self.transcription_interface and self.transcription_interface.is_processing:
            self.start_button.setEnabled(False)
        else:
            self.start_button.setEnabled(True)
        self.update_thumbnail(video_info.thumbnail_path)
        if self.transcription_interface:
            self.transcription_interface._media_signature = file_signature(video_info.file_path)
            self.transcription_interface._save_transcription_state()

    def update_thumbnail(self, thumbnail_path):
        """更新视频缩略图"""
        if not Path(thumbnail_path).exists():
            thumbnail_path = RESOURCE_PATH / "assets" / "audio-thumbnail.png"

        pixmap = QPixmap(str(thumbnail_path)).scaled(
            self.video_thumbnail.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.video_thumbnail.setPixmap(pixmap)

    def setup_signals(self):
        self.start_button.clicked.connect(self.on_start_button_clicked)

    def show_language_settings(self):
        """显示语言设置对话框"""
        dialog = LanguageSettingDialog(self.window())
        if dialog.exec_():
            return True
        return False

    def on_start_button_clicked(self):
        """开始转录按钮点击事件"""
        force_no_asr_cache = self.start_button.text() != self.tr("开始转录")
        if self.task and not self.task.need_next_task:
            need_language_settings = cfg.transcribe_model.value == TranscribeModelEnum.WHISPER_X
            if need_language_settings and not self.show_language_settings():
                return
        self.start_transcription(force_no_asr_cache=force_no_asr_cache)

    def start_transcription(self, need_create_task=True, force_no_asr_cache=False):
        """开始转录过程"""
        if need_create_task:
            if self.video_info is None:
                self._show_transcription_preflight_error(
                    self.tr("请先导入可用的音频或视频文件")
                )
                return False
            self.task = TaskFactory.create_transcribe_task(self.video_info.file_path)

        from app.core.utils.transcription_model_utils import (
            validate_transcription_model_ready,
        )

        model_ready, model_message = validate_transcription_model_ready(
            self.task.transcribe_config if self.task else None
        )
        if not model_ready:
            self._show_transcription_preflight_error(model_message)
            return False

        self.transcription_interface.is_processing = True
        self.transcription_interface._set_translation_handoff_enabled(False)
        self.progress_ring.show()
        self.progress_ring.setValue(100)
        self.start_button.setEnabled(False)
        self.start_button.setText(self.tr("正在准备模型…"))

        if self.task and self.task.output_path and Path(self.task.output_path).exists():
            force_no_asr_cache = True

        if force_no_asr_cache and self.task and self.task.transcribe_config:
            self.task.transcribe_config.use_asr_cache = False

        from app.thread.transcript_thread_clean import TranscriptThread

        self.transcript_thread = TranscriptThread(self.task)
        self.transcript_thread.finished.connect(self.on_transcript_finished)
        self.transcript_thread.progress.connect(self.on_transcript_progress)
        self.transcript_thread.error.connect(self.on_transcript_error)
        self.transcription_interface._transcription_status = "processing"
        self.transcription_interface._save_transcription_state()
        self.transcript_thread.start()
        return True

    def _show_transcription_preflight_error(self, message: str):
        if self.transcription_interface:
            self.transcription_interface.is_processing = False
            self.transcription_interface._set_translation_handoff_enabled(False)
        self.progress_ring.hide()
        self.progress_ring.setValue(0)
        self.start_button.setEnabled(bool(self.video_info or self.task))
        self.start_button.setText(self.tr("开始转录"))
        InfoBar.warning(
            self.tr("转录模型不可用"),
            self.tr(str(message)),
            duration=6000,
            parent=self.transcription_interface or self,
        )

    def on_transcript_progress(self, value, message):
        """更新转录进度"""
        self.start_button.setText(message)
        self.progress_ring.setValue(value)
        self.transcription_interface._save_transcription_state()

    def on_transcript_error(self, error):
        """处理转录错误"""
        if self.transcription_interface:
            self.transcription_interface.is_processing = False
            self.transcription_interface._set_translation_handoff_enabled(False)
        self.progress_ring.hide()
        self.progress_ring.setValue(0)
        self.start_button.setEnabled(True)
        self.start_button.setText(self.tr("重新转录"))
        self.transcription_interface._transcription_status = "interrupted"
        self.transcription_interface._save_transcription_state()
        InfoBar.error(
            self.tr("转录失败"),
            self.tr(error),
            duration=3000,
            parent=self.parent().parent(),
        )
        send_desktop_notification(
            self.tr("转录失败"),
            str(error),
            target="transcription",
        )

    def on_transcript_finished(self, task):
        """转录完成处理"""
        self.start_button.setEnabled(True)
        self.start_button.setText(self.tr("转录完成"))
        self.finished.emit(task)

    def reset_ui(self):
        """重置UI状态"""
        self.start_button.setDisabled(False)
        self.start_button.setText(self.tr("开始转录"))
        self.progress_ring.setValue(0)

    def set_task(self, task):
        """设置任务并更新UI"""
        self.task = task
        self.reset_ui()

    def stop(self):
        if hasattr(self, "transcript_thread"):
            self.transcript_thread.terminate()


class TranscriptionInterface(QWidget):
    """转录界面类,用于显示视频信息和转录进度"""

    finished = pyqtSignal(str, str)
    send_to_translate = pyqtSignal(str, str)
    TRANSCRIPTION_STATE_PATH = APP_DATA_PATH / "transcription_state.json"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("TranscriptionInterface")
        self.setAcceptDrops(True)
        self.task = None
        self.is_processing = False
        self._transcription_status = "ready"
        self._restoring_state = False
        self._media_signature = None

        self._init_ui()
        self._setup_signals()
        self._set_value()
        cfg.themeMode.valueChanged.connect(lambda *_: self._apply_theme_styles())
        self._apply_theme_styles()
        self._restore_transcription_state()
        QApplication.instance().aboutToQuit.connect(self._save_transcription_state)

    def _save_transcription_state(self):
        if self._restoring_state:
            return
        card = self.video_info_card
        info = card.video_info
        if not info:
            return
        task = card.task or self.task
        write_page_state(self.TRANSCRIPTION_STATE_PATH, {
            "media": asdict(info),
            "signature": self._media_signature,
            "status": self._transcription_status,
            "progress": card.progress_ring.value(),
            "output_path": task.output_path if task else None,
        })

    def _restore_transcription_state(self):
        payload = read_page_state(self.TRANSCRIPTION_STATE_PATH)
        try:
            info = VideoInfo(**payload["media"])
            if not all(isinstance(getattr(info, key), str) for key in ("file_path", "file_name", "thumbnail_path")):
                return
            if not isinstance(info.duration_seconds, (int, float)) or not 0 <= info.duration_seconds < 10**9:
                return
            signature = file_signature(info.file_path)
            if not signature or signature != payload.get("signature"):
                return
            progress = max(0, min(100, int(payload.get("progress", 0))))
            output_path = payload.get("output_path")
            if payload.get("status") not in ("ready", "processing", "interrupted", "completed"):
                return
            if output_path is not None and not isinstance(output_path, str):
                return
        except (TypeError, ValueError, KeyError, OverflowError):
            return
        self._restoring_state = True
        try:
            self.video_info_card.update_info(info)
            self._transcription_status = payload.get("status", "ready")
            if self._transcription_status == "completed" and output_path and Path(output_path).is_file():
                self.task = TranscribeTask(file_path=info.file_path, output_path=output_path)
                self.video_info_card.task = self.task
                self.video_info_card.start_button.setText(self.tr("转录完成"))
                self._set_translation_handoff_enabled(True)
            elif self._transcription_status in {"processing", "interrupted"}:
                self._transcription_status = "interrupted"
                self.video_info_card.start_button.setText(self.tr("重新转录"))
                self.video_info_card.start_button.setToolTip(
                    self.tr("已恢复上次媒体和进度；点击后重新执行转录")
                )
            elif self._transcription_status == "completed":
                self._transcription_status = "ready"
                progress = 0
                self.video_info_card.start_button.setToolTip(self.tr("原字幕文件不存在，请重新转录"))
            self.video_info_card.progress_ring.setValue(progress)
            self.video_info_card.progress_ring.setVisible(progress > 0)
        finally:
            self._restoring_state = False

    def _init_ui(self):
        """初始化UI"""
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setObjectName("main_layout")
        self.main_layout.setSpacing(20)

        # 添加命令栏
        self._setup_command_bar()

        self.scroll_area = TrackpadFriendlyScrollArea(orient=Qt.Vertical, parent=self)
        self.scroll_area.setStyleSheet(
            "QScrollArea{background: transparent; border: none}"
        )
        self.scroll_content = QWidget(self)
        self.scroll_content.setStyleSheet("QWidget{background: transparent}")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(20)

        self.video_info_card = VideoInfoCard(self)
        self.video_info_card.setObjectName("videoInfoPanel")
        self.scroll_layout.addWidget(self.video_info_card)

        # 添加转录设置卡片
        self.transcription_setting_card = TranscriptionSettingCard(self)
        self.transcription_setting_card.setObjectName("transcriptionSettingPanel")
        self.scroll_layout.addWidget(self.transcription_setting_card)
        self.scroll_layout.addStretch(1)

        self.scroll_area.setWidget(self.scroll_content)
        self.scroll_area.setWidgetResizable(True)
        self.main_layout.addWidget(self.scroll_area, 1)

    def _setup_command_bar(self):
        """设置命令栏"""
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.command_bar.setFixedHeight(40)

        # 添加打开文件按钮
        self.open_file_action = Action(FluentIcon.FOLDER, self.tr("打开文件"))
        self.open_file_action.triggered.connect(self._on_file_select)
        self.command_bar.addAction(self.open_file_action)

        self.command_bar.addSeparator()

        # 添加转录模型选择按钮
        self.model_button = TransparentDropDownPushButton(
            self.tr("转录模型"), self, FluentIcon.SETTING
        )
        self.model_button.setFixedHeight(34)
        self.model_button.setMinimumWidth(180)

        self.model_menu = RoundMenu(parent=self)
        model_options = cfg.transcribe_model.validator.options
        for model in model_options:
            if "接口" in model.value or "api" in model.value.lower():
                self.model_menu.addActions(
                    [
                        Action(FluentIcon.GLOBE, model.value),
                    ]
                )
            else:
                self.model_menu.addActions(
                    [
                        Action(FluentIcon.ROBOT, model.value),
                    ]
                )
        self.model_button.setMenu(self.model_menu)
        self.command_bar.addWidget(self.model_button)

        self.send_to_translate_action = Action(
            FluentIcon.LANGUAGE,
            self.tr("送去翻译"),
            triggered=self._on_send_to_translate_clicked,
        )
        self.command_bar.addAction(self.send_to_translate_action)
        self._set_translation_handoff_enabled(False)

        self.main_layout.addWidget(self.command_bar)

    def _apply_theme_styles(self):
        if isDarkTheme():
            page_background = "#202124"
            panel_background = "rgba(255, 255, 255, 0.05)"
            panel_border = "rgba(255, 255, 255, 0.08)"
            thumbnail_background = "#1E1F22"
            thumbnail_border = "rgba(255, 255, 255, 0.08)"
        else:
            page_background = "#F5F7FA"
            panel_background = "#FFFFFF"
            panel_border = "rgba(17, 24, 39, 0.12)"
            thumbnail_background = "#F8FAFC"
            thumbnail_border = "rgba(17, 24, 39, 0.12)"

        self.setStyleSheet(
            f"""
            QWidget#TranscriptionInterface {{
                background-color: {page_background};
            }}
            QWidget#videoInfoPanel, QWidget#transcriptionSettingPanel {{
                background-color: {panel_background};
                border: 1px solid {panel_border};
                border-radius: 8px;
            }}
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            """
        )
        self.video_info_card.video_thumbnail.setStyleSheet(
            f"""
            QLabel {{
                background-color: {thumbnail_background};
                border: 1px solid {thumbnail_border};
                border-radius: 6px;
            }}
            """
        )

    def _setup_signals(self):
        """设置信号连接"""
        self.video_info_card.finished.connect(self._on_transcript_finished)

        # 设置模型选择菜单的信号连接
        for action in self.model_menu.actions():
            action.triggered.connect(
                lambda checked, text=action.text(): self.on_transcription_model_changed(
                    text
                )
            )

        # 全局信号连接
        signalBus.transcription_model_changed.connect(
            self.on_transcription_model_changed
        )

    def _set_value(self):
        """设置转录模型"""
        model_name = cfg.get(cfg.transcribe_model).value
        # self.model_button.setText(self.tr(model_name))
        self.on_transcription_model_changed(model_name)

    def on_transcription_model_changed(self, model_name: str):
        """处理转录模型改变"""
        self.model_button.setText(self.tr(model_name))
        self.transcription_setting_card.on_model_changed(model_name)
        for model in cfg.transcribe_model.validator.options:
            if model.value == model_name:
                cfg.set(cfg.transcribe_model, model)
                break

    def _on_transcript_finished(self, task: TranscribeTask):
        """转录完成处理"""
        self.is_processing = False
        self.task = task
        self.video_info_card.task = task
        self._transcription_status = "completed"
        self.video_info_card.progress_ring.setValue(100)
        self.video_info_card.progress_ring.hide()
        self._save_transcription_state()
        self._set_translation_handoff_enabled(True)
        send_desktop_notification(
            self.tr("转录完成"),
            self.tr("字幕文件已生成：") + Path(task.output_path).name,
            target="transcription",
        )
        if task.need_next_task:
            self.finished.emit(task.output_path, task.file_path)

            InfoBar.success(
                self.tr("转录完成"),
                self.tr("开始字幕优化..."),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self.parent(),
            )

    def _on_send_to_translate_clicked(self):
        """将已生成的转录字幕发送到字幕优化与翻译页。"""
        if self.is_processing:
            InfoBar.warning(
                self.tr("提示"),
                self.tr("正在处理中，请等待当前任务完成"),
                duration=3000,
                parent=self,
            )
            return

        task = self.video_info_card.task or self.task
        subtitle_path = Path(task.output_path) if task and task.output_path else None
        if not task or not subtitle_path or not subtitle_path.exists():
            InfoBar.warning(
                self.tr("提示"),
                self.tr("请先完成转录后再送去翻译"),
                duration=3000,
                parent=self,
            )
            return

        self.send_to_translate.emit(str(subtitle_path), task.file_path or "")
        InfoBar.success(
            self.tr("已发送"),
            self.tr("已将字幕发送到字幕优化与翻译页面。"),
            duration=2500,
            parent=self,
        )

    def _on_file_select(self):
        """文件选择处理"""
        default_dir = str(WORK_PATH)
        file_dialog = QFileDialog()

        video_formats = " ".join(f"*.{fmt.value}" for fmt in SupportedVideoFormats)
        audio_formats = " ".join(f"*.{fmt.value}" for fmt in SupportedAudioFormats)
        filter_str = f"{self.tr('媒体文件')} ({video_formats} {audio_formats});;{self.tr('视频文件')} ({video_formats});;{self.tr('音频文件')} ({audio_formats})"

        file_path, _ = file_dialog.getOpenFileName(
            self, self.tr("选择媒体文件"), default_dir, filter_str
        )
        if file_path:
            self._clear_current_task()
            self.update_info(file_path)

    def _clear_current_task(self):
        """清理旧任务，避免新文件复用上一份转录结果。"""
        self.task = None
        self.video_info_card.task = None
        self.video_info_card.video_info = None
        self._media_signature = None
        self._transcription_status = "ready"
        self.video_info_card.progress_ring.hide()
        self.video_info_card.progress_ring.setValue(0)
        self.video_info_card.start_button.setToolTip("")
        try:
            self.TRANSCRIPTION_STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        self.video_info_card.start_button.setEnabled(False)
        self.video_info_card.start_button.setText(self.tr("开始转录"))
        self._set_translation_handoff_enabled(False)

    def _set_translation_handoff_enabled(self, enabled: bool):
        self.send_to_translate_action.setEnabled(bool(enabled))
        self.send_to_translate_action.setToolTip(
            self.tr("将已生成的字幕送到字幕优化与翻译页")
            if enabled
            else self.tr("请先完成转录")
        )

    def update_info(self, file_path):
        """设置UI"""
        from app.thread.video_info_thread import VideoInfoThread

        if self.video_info_card.video_info and self.video_info_card.video_info.file_path != file_path:
            self._clear_current_task()
        self.video_info_thread = VideoInfoThread(file_path)
        self.video_info_thread.finished.connect(self.video_info_card.update_info)
        self.video_info_thread.error.connect(self._on_video_info_error)
        self.video_info_thread.start()

    def _on_video_info_error(self, error_msg):
        """处理视频信息提取错误"""
        self.is_processing = False
        InfoBar.error(self.tr("错误"), self.tr(error_msg), duration=3000, parent=self)

    def set_task(self, task: TranscribeTask):
        """设置任务并更新UI"""
        self._clear_current_task()
        self.task = task
        self.video_info_card.set_task(self.task)
        output_path = Path(task.output_path) if task.output_path else None
        if output_path and output_path.is_file():
            self._transcription_status = "completed"
        self._set_translation_handoff_enabled(
            bool(output_path and output_path.exists())
        )
        self.update_info(self.task.file_path)

    def process(self):
        """主处理函数"""
        self._set_translation_handoff_enabled(False)
        return self.video_info_card.start_transcription(need_create_task=False)

    def dragEnterEvent(self, event):
        """拖拽进入事件处理"""
        event.accept() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event):
        """拖拽放下事件处理"""
        if self.is_processing:

            InfoBar.warning(
                self.tr("警告"),
                self.tr("正在处理中，请等待当前任务完成"),
                duration=3000,
                parent=self,
            )
            return

        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file_path in files:
            if not os.path.isfile(file_path):
                continue

            file_ext = os.path.splitext(file_path)[1][1:].lower()

            # 检查文件格式是否支持
            supported_formats = {fmt.value for fmt in SupportedVideoFormats} | {
                fmt.value for fmt in SupportedAudioFormats
            }
            is_supported = file_ext in supported_formats

            if is_supported:
                self._clear_current_task()
                self.update_info(file_path)
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr("开始语音转文字"),
                    duration=3000,
                    parent=self,
                )
                break
            else:
                InfoBar.error(
                    self.tr(f"格式错误") + file_ext,
                    self.tr(f"请拖入音频或视频文件"),
                    duration=3000,
                    parent=self,
                )

    def closeEvent(self, event):
        self._save_transcription_state()
        self.video_info_card.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    window = TranscriptionInterface()
    window.show()
    sys.exit(app.exec_())
