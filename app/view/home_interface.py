from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import SegmentedWidget, ToolButton, isDarkTheme

from app.common.config import cfg
from app.core.task_factory import TaskFactory
from app.view.download_center_interface import DownloadCenterInterface
from app.view.log_window import LogWindow
from app.view.setting_interface import SettingInterface
from app.view.subtitle_interface import SubtitleInterface
from app.view.transcription_interface import TranscriptionInterface


class HomeInterface(QWidget):
    github_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setObjectName("HomeInterface")

        # 创建分段控件、右侧工具按钮和堆叠控件
        self.pivot = SegmentedWidget(self)
        self.pivot.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.log_window = None
        self.log_button = ToolButton(FIF.DOCUMENT, self)
        self.log_button.setToolTip(self.tr("查看日志"))
        self.github_button = ToolButton(FIF.GITHUB, self)
        self.github_button.setToolTip("GitHub")
        for button in (self.log_button, self.github_button):
            button.setFixedSize(34, 34)

        self.stackedWidget = QStackedWidget(self)
        self.vBoxLayout = QVBoxLayout(self)
        self.topLayout = QHBoxLayout()

        # 添加子界面
        self.download_center_interface = DownloadCenterInterface(self)
        self.transcription_interface = TranscriptionInterface(self)
        self.subtitle_optimization_interface = SubtitleInterface(self)
        self.setting_interface = SettingInterface(self)

        self._add_workspace_page(
            self.download_center_interface,
            "DownloadCenterInterface",
            self.tr("下载中心"),
        )
        self._add_workspace_page(
            self.transcription_interface, "TranscriptionInterface", self.tr("语音转录")
        )
        self._add_workspace_page(
            self.subtitle_optimization_interface,
            "SubtitleInterface",
            self.tr("字幕优化与翻译"),
        )
        self._add_workspace_page(
            self.setting_interface,
            "SettingInterface",
            self.tr("设置"),
        )
        self.topLayout.addWidget(self.pivot, 0, Qt.AlignVCenter)
        self.topLayout.addStretch(1)
        self.topLayout.addWidget(self.log_button, 0, Qt.AlignVCenter)
        self.topLayout.addWidget(self.github_button, 0, Qt.AlignVCenter)

        self.vBoxLayout.addLayout(self.topLayout)
        self.vBoxLayout.addWidget(self.stackedWidget)
        self.vBoxLayout.setContentsMargins(30, 10, 30, 30)

        self.stackedWidget.currentChanged.connect(self.onCurrentIndexChanged)
        self.stackedWidget.setCurrentWidget(self.download_center_interface)
        self.pivot.setCurrentItem("DownloadCenterInterface")

        self.download_center_interface.send_to_transcription.connect(
            self.open_transcription
        )
        self.transcription_interface.finished.connect(
            self.switch_to_subtitle_optimization
        )
        self.transcription_interface.send_to_translate.connect(
            self.open_subtitle_optimization
        )
        self.log_button.clicked.connect(self.show_log_window)
        self.github_button.clicked.connect(self.github_requested)
        cfg.themeMode.valueChanged.connect(lambda *_: self._apply_theme_styles())
        self._apply_theme_styles()

    def _apply_theme_styles(self):
        if isDarkTheme():
            background_color = "#202124"
            border_color = "rgba(255, 255, 255, 0.08)"
        else:
            background_color = "#F5F7FA"
            border_color = "rgba(17, 24, 39, 0.10)"

        self.setStyleSheet(
            f"""
            QWidget#HomeInterface {{
                background-color: {background_color};
                border-top: 1px solid {border_color};
            }}
            QStackedWidget {{
                background-color: transparent;
                border: none;
            }}
            """
        )

    def show_download_center_page(self):
        self.stackedWidget.setCurrentWidget(self.download_center_interface)
        self.pivot.setCurrentItem("DownloadCenterInterface")

    def switch_to_transcription(self, file_path):
        # 切换到转录界面
        transcribe_task = TaskFactory.create_transcribe_task(
            file_path, need_next_task=True
        )
        if self.transcription_interface.set_task(transcribe_task) is False:
            return False
        self.transcription_interface.process()
        self.stackedWidget.setCurrentWidget(self.transcription_interface)
        self.pivot.setCurrentItem("TranscriptionInterface")

    def open_transcription(self, file_path: str, need_next_task: bool = False):
        transcribe_task = TaskFactory.create_transcribe_task(
            file_path, need_next_task=need_next_task
        )
        if self.transcription_interface.set_task(transcribe_task) is False:
            return False
        self.show_transcription_page()

    def show_transcription_page(self):
        self.stackedWidget.setCurrentWidget(self.transcription_interface)
        self.pivot.setCurrentItem("TranscriptionInterface")

    def switch_to_subtitle_optimization(self, file_path, video_path):
        # 切换到字幕处理界面
        subtitle_task = TaskFactory.create_subtitle_task(
            file_path, video_path, need_next_task=False
        )
        if self.subtitle_optimization_interface.set_task(subtitle_task) is False:
            return False
        self.subtitle_optimization_interface.process()
        self.show_subtitle_optimization_page()

    def open_subtitle_optimization(self, file_path, video_path):
        # 只加载字幕并切换页面，不自动开始字幕处理
        if not file_path or not Path(file_path).exists():
            raise FileNotFoundError(f"字幕文件不存在: {file_path}")
        subtitle_task = TaskFactory.create_subtitle_task(
            file_path, video_path, need_next_task=False
        )
        if self.subtitle_optimization_interface.set_task(subtitle_task) is False:
            return False
        self.show_subtitle_optimization_page()

    def show_subtitle_optimization_page(self):
        self.stackedWidget.setCurrentWidget(self.subtitle_optimization_interface)
        self.pivot.setCurrentItem("SubtitleInterface")

    def show_setting_page(self):
        self.stackedWidget.setCurrentWidget(self.setting_interface)
        self.pivot.setCurrentItem("SettingInterface")

    def show_log_window(self):
        if self.log_window is None:
            self.log_window = LogWindow(self.window())
        if self.log_window.isHidden():
            self.log_window.show()
        else:
            self.log_window.activateWindow()

    def _add_workspace_page(self, widget, objectName, text):
        # 添加子界面到堆叠控件和分段控件
        widget.setObjectName(objectName)
        self.stackedWidget.addWidget(widget)
        self.pivot.addItem(
            routeKey=objectName,
            text=text,
            onClick=lambda: self.stackedWidget.setCurrentWidget(widget),
        )

    def onCurrentIndexChanged(self, index):
        # 当堆叠控件的当前索引改变时，更新分段控件的当前项
        widget = self.stackedWidget.widget(index)
        if widget:
            self.pivot.setCurrentItem(widget.objectName())

    def closeEvent(self, event):
        # 关闭事件，关闭所有子界面
        self.download_center_interface.close()
        self.transcription_interface.close()
        self.subtitle_optimization_interface.close()
        self.setting_interface.close()
        super().closeEvent(event)
