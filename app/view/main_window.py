import os

import psutil
from PyQt5.QtCore import QEvent, QRect, Qt, QSize, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import QApplication
from qframelesswindow.utils import startSystemMove
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    SplashScreen,
    isDarkTheme,
)

from app.common.config import cfg
from app.core.utils.platform_utils import IS_MACOS, DEFAULT_COOKIE_BROWSER_LABEL
from app.common.signal_bus import signalBus
from app.config import ASSETS_PATH, GITHUB_REPO_URL
from app.thread.browser_cookie_export_thread import BrowserCookieExportThread
from app.view.home_interface import HomeInterface

LOGO_PATH = ASSETS_PATH / "logo.png"
MAC_TRAFFIC_LIGHT_ROW_HEIGHT = 32
MAC_TITLE_BAR_HEIGHT = 48
MAC_CHROME_LEFT_PADDING = 14
MAC_TRAFFIC_LIGHT_BUTTON_AREA_WIDTH = 96


class MainWindow(FluentWindow):

    def __init__(self):
        super().__init__()
        self._mac_drag_widgets = ()
        self._closing = False
        self._startup_cookie_export_attempted = False
        self.startupCookieExportThread = None
        self._disableNavigationInterface()
        self._styleMacTitleBarBrand()
        self._installMacDragEventFilters()
        cfg.themeMode.valueChanged.connect(lambda *_: self._applyMacWindowChromeStyle())
        self.initWindow()

        # 创建子界面
        self.homeInterface = HomeInterface(self)
        self.downloadCenterInterface = self.homeInterface.download_center_interface
        self.settingInterface = self.homeInterface.setting_interface
        self.homeInterface.github_requested.connect(self.onGithubDialog)
        signalBus.notification_clicked.connect(self.on_notification_clicked)

        # 初始化主工作台
        self.initWorkspace()
        self.splashScreen.finish()
        QTimer.singleShot(0, self._start_cookie_extraction_on_startup)

        # 注册退出处理， 清理进程
        import atexit

        atexit.register(self.stop)

    def initWorkspace(self):
        """初始化单一工作台界面"""
        self.stackedWidget.addWidget(self.homeInterface)
        self.switchTo(self.homeInterface)

    def switchTo(self, interface):
        if interface.windowTitle():
            self.setWindowTitle(interface.windowTitle())
        else:
            self.setWindowTitle(self.tr("卡卡字幕助手 -- VideoCaptioner"))
        self.stackedWidget.setCurrentWidget(interface, popOut=False)

    def _start_cookie_extraction_on_startup(self) -> bool:
        if self._closing or self._startup_cookie_export_attempted:
            return False
        self._startup_cookie_export_attempted = True

        if not bool(cfg.get(cfg.download_auto_extract_cookies_on_startup)):
            return False

        browser = str(cfg.get(cfg.download_cookie_browser) or DEFAULT_COOKIE_BROWSER_LABEL)
        thread = BrowserCookieExportThread(browser, self)
        thread.completed.connect(self._on_startup_cookie_export_completed)
        thread.finished.connect(self._release_startup_cookie_export_thread)
        self.startupCookieExportThread = thread
        self.settingInterface.set_cookie_export_in_progress(True)
        thread.start()
        return True

    def _on_startup_cookie_export_completed(self, result: dict):
        self.settingInterface.refresh_cookie_status(result)
        if self._closing:
            return
        message = result.get(
            "message", self.tr("无法从浏览器自动提取 Cookie")
        )
        if result.get("success"):
            InfoBar.success(
                self.tr("Cookie 自动提取完成"),
                message,
                duration=2500,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
            )
            return

        InfoBar.warning(
            self.tr("Cookie 自动提取失败"),
            message,
            duration=5000,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
        )

    def _release_startup_cookie_export_thread(self):
        thread = self.startupCookieExportThread
        self.startupCookieExportThread = None
        self.settingInterface.set_cookie_export_in_progress(False)
        if thread is not None:
            thread.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def systemTitleBarRect(self, size: QSize) -> QRect:
        """Place native macOS traffic-light buttons on the left."""
        return QRect(0, 0 if self.isFullScreen() else 8, 75, size.height())

    def _macTrafficLightRowHeight(self) -> int:
        if not IS_MACOS or self.isFullScreen():
            return 0
        return MAC_TRAFFIC_LIGHT_ROW_HEIGHT

    def _macContentTopMargin(self) -> int:
        title_bar_height = self.titleBar.height() or MAC_TITLE_BAR_HEIGHT
        return self._macTrafficLightRowHeight() + title_bar_height

    def _disableNavigationInterface(self):
        """Keep FluentWindow internals but remove the left navigation rail."""
        if not hasattr(self.navigationInterface, "panel"):
            return

        self.navigationInterface.hide()
        self.navigationInterface.setFixedWidth(0)
        self.navigationInterface.setMinimumWidth(0)
        self.navigationInterface.panel.hide()
        self.navigationInterface.panel.setReturnButtonVisible(False)
        self.navigationInterface.panel.returnButton.setDisabled(True)
        self.navigationInterface.panel.setMenuButtonVisible(False)
        self.navigationInterface.panel.setFixedWidth(0)

    def _styleMacTitleBarBrand(self):
        """Align title-bar branding with the compact navigation icon column."""
        if hasattr(self.titleBar, "hBoxLayout"):
            self.titleBar.hBoxLayout.setContentsMargins(
                MAC_CHROME_LEFT_PADDING, 0, 0, 0
            )
            self.titleBar.hBoxLayout.setSpacing(8)

        if hasattr(self.titleBar, "titleLabel"):
            self.titleBar.titleLabel.setStyleSheet(
                "font-size: 16px; font-weight: 600;"
            )

    def _installMacDragEventFilters(self):
        if not IS_MACOS:
            return
        widgets = [
            self.titleBar,
        ]
        for attr_name in ("iconLabel", "titleLabel"):
            widget = getattr(self.titleBar, attr_name, None)
            if widget is not None:
                widgets.append(widget)

        self._mac_drag_widgets = tuple(widgets)
        for widget in self._mac_drag_widgets:
            widget.installEventFilter(self)

    def _reserveMacTitleBarSpace(self):
        """Reserve a dedicated macOS traffic-light row above app chrome."""
        traffic_light_row_height = self._macTrafficLightRowHeight()
        self.titleBar.move(0, traffic_light_row_height)

        content_top_margin = self._macContentTopMargin()
        self.widgetLayout.setContentsMargins(0, content_top_margin, 0, 0)

    def _reserveMacTitleBarSpaceForNavigation(self):
        """Prevent navigation from occupying the native macOS title bar area."""
        self._reserveMacTitleBarSpace()

    def _applyMacWindowChromeStyle(self):
        """Use Qt translucent chrome without covering the main content."""
        if not IS_MACOS:
            return
        if isDarkTheme():
            title_bar_style = (
                "background: rgba(26, 27, 30, 0.84);"
                "border-bottom: 1px solid rgba(255, 255, 255, 0.08);"
            )
        else:
            title_bar_style = (
                "background: rgba(255, 255, 255, 0.88);"
                "border-bottom: 1px solid rgba(17, 24, 39, 0.10);"
            )

        self.titleBar.setObjectName("macTitleBarChrome")
        self.titleBar.setAttribute(Qt.WA_TranslucentBackground, True)
        self.titleBar.setStyleSheet(
            f"QWidget#macTitleBarChrome {{ {title_bar_style} }}"
        )

    def _isMacTrafficLightRowDragPoint(self, pos) -> bool:
        return (
            not self.isFullScreen()
            and pos.y() < self._macTrafficLightRowHeight()
            and pos.x() > MAC_TRAFFIC_LIGHT_BUTTON_AREA_WIDTH
        )

    def _startMacWindowDrag(self, global_pos):
        if self.isFullScreen():
            return False

        startSystemMove(self, global_pos)
        return True

    def initWindow(self):
        """初始化窗口"""
        self.resize(1200, 800)
        self.setMinimumWidth(700)
        self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.setWindowTitle(self.tr("卡卡字幕助手 -- VideoCaptioner"))

        # 创建启动画面
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.splashScreen.raise_()

        # 设置窗口位置, 居中
        desktop = QApplication.desktop().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        self.show()
        self._applyMacWindowChromeStyle()
        QApplication.processEvents()

    def onGithubDialog(self):
        """打开GitHub"""
        w = MessageBox(
            self.tr("GitHub信息"),
            self.tr(
                "VideoCaptioner 由本人在课余时间独立开发完成，目前托管在GitHub上，欢迎Star和Fork。项目诚然还有很多地方需要完善，遇到软件的问题或者BUG欢迎提交Issue。\n\n https://github.com/WEIFENG2333/VideoCaptioner"
            ),
            self,
        )
        w.yesButton.setText(self.tr("打开 GitHub"))
        w.cancelButton.hide()
        if w.exec():
            QDesktopServices.openUrl(QUrl(GITHUB_REPO_URL))

    def on_notification_clicked(self, target: str):
        target = str(target or "").strip()
        if target == "download_center":
            self.switchTo(self.homeInterface)
            self.homeInterface.show_download_center_page()
        elif target == "transcription":
            self.switchTo(self.homeInterface)
            self.homeInterface.show_transcription_page()
        elif target == "subtitle":
            self.switchTo(self.homeInterface)
            self.homeInterface.show_subtitle_optimization_page()
        else:
            return
        self._activate_from_notification()

    def _activate_from_notification(self):
        if self.windowState() & Qt.WindowMinimized:
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reserveMacTitleBarSpaceForNavigation()
        if hasattr(self, "splashScreen"):
            self.splashScreen.resize(self.size())

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and self._isMacTrafficLightRowDragPoint(event.pos())
            and self._startMacWindowDrag(event.globalPos())
        ):
            event.accept()
            return

        super().mousePressEvent(event)

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.MouseButtonPress
            and event.button() == Qt.LeftButton
            and obj in getattr(self, "_mac_drag_widgets", ())
        ):
            if obj is self.titleBar and hasattr(self.titleBar, "canDrag"):
                if not self.titleBar.canDrag(event.pos()):
                    return super().eventFilter(obj, event)

            if self._startMacWindowDrag(event.globalPos()):
                event.accept()
                return True

        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        self._closing = True
        thread = self.startupCookieExportThread
        if thread is not None and thread.isRunning():
            event.ignore()
            self.hide()
            return

        # 关闭所有子界面
        # self.homeInterface.close()
        # self.settingInterface.close()
        super().closeEvent(event)

        # 强制退出应用程序
        QApplication.quit()

        # 确保所有线程和进程都被终止 要是一些错误退出就不会处理了。
        # import os
        # os._exit(0)

    def stop(self):
        thread = self.startupCookieExportThread
        if thread is not None and thread.isRunning():
            thread.wait()

        # 找到 FFmpeg 进程并关闭
        process = psutil.Process(os.getpid())
        for child in process.children(recursive=True):
            child.kill()
