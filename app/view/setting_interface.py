from pathlib import Path

from PyQt5.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QApplication, QFileDialog, QLabel, QSizePolicy, QWidget
from qfluentwidgets import ComboBoxSettingCard, CustomColorSettingCard, ExpandLayout
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    HyperlinkCard,
    InfoBar,
    MessageBox,
    MessageBoxBase,
    OptionsSettingCard,
    PrimaryPushSettingCard,
    PushButton,
    PushSettingCard,
    RangeSettingCard,
    ScrollArea,
    SettingCardGroup,
    SwitchSettingCard,
    TextEdit,
    isDarkTheme,
    setTheme,
    setThemeColor,
)

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.components.EditComboBoxSettingCard import EditComboBoxSettingCard
from app.components.LineEditSettingCard import LineEditSettingCard
from app.components.SpinBoxSettingCard import SpinBoxSettingCard
from app.components.model_download_dialog import ModelDownloadDialog
from app.config import AUTHOR, FEEDBACK_URL, HELP_URL, YEAR
from app.core.entities import LLMServiceEnum, TranscribeModelEnum
from app.core.subtitle_processor.prompt import (
    PROMPT_CENTER_ITEMS,
    PROMPT_TERM_GLOSSARY,
    get_default_prompt_template,
    get_prompt_config_attr,
    get_prompt_template,
    get_required_prompt_variables,
    validate_prompt_template,
)
from app.core.utils.desktop_notification import (
    get_desktop_notification_status,
    request_desktop_notification_authorization,
    send_desktop_notification,
)
from app.core.utils.download_description import (
    DEFAULT_DESCRIPTION_TEMPLATE,
    DESCRIPTION_TEMPLATE_VARIABLES,
    find_unknown_template_variables,
)
from app.core.utils.transcript_terms import extract_glossary_pairs, format_glossary_pairs
from app.core.utils.proxy_utils import (
    PROXY_MODE_MANUAL,
    PROXY_MODE_OFF,
    get_effective_download_proxy_url,
)


class DefaultPromptDialog(MessageBoxBase):
    def __init__(self, title: str, content: str, parent=None):
        super().__init__(parent)
        max_width, edit_height = self._dialog_dimensions(parent)
        self.widget.setMaximumWidth(max_width)
        self.widget.setMinimumWidth(min(max_width, 720))
        self.titleLabel = BodyLabel(title, self)
        self.textEdit = TextEdit(self)
        self.textEdit.setReadOnly(True)
        self.textEdit.setPlainText(content)
        self.textEdit.setMinimumSize(min(max_width - 80, 640), edit_height)
        self.textEdit.setMaximumHeight(edit_height)
        self.textEdit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.textEdit)
        self.yesButton.setText(self.tr("关闭"))
        self.cancelButton.hide()

    @staticmethod
    def _dialog_dimensions(parent=None) -> tuple[int, int]:
        if parent is not None and parent.width() > 0:
            width = min(920, max(700, parent.width() - 160))
            height = min(420, max(320, parent.height() - 260))
        else:
            screen = QApplication.desktop().availableGeometry()
            width = min(920, max(700, screen.width() - 240))
            height = min(420, max(320, screen.height() - 320))
        return width, height


class PromptCenterDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        dialog_width, self._editor_height = self._dialog_dimensions(parent)
        self.widget.setMaximumWidth(dialog_width)
        self.widget.setMinimumWidth(min(dialog_width, 760))
        self.prompt_items = PROMPT_CENTER_ITEMS
        self.current_prompt_id = self.prompt_items[0]["id"]
        self.setup_ui()
        self.load_prompt(self.current_prompt_id)

    @staticmethod
    def _dialog_dimensions(parent=None) -> tuple[int, int]:
        if parent is not None and parent.width() > 0:
            width = min(960, max(720, parent.width() - 160))
            height = min(420, max(300, parent.height() - 340))
        else:
            screen = QApplication.desktop().availableGeometry()
            width = min(960, max(720, screen.width() - 240))
            height = min(420, max(300, screen.height() - 400))
        return width, height

    def setup_ui(self):
        self.setWindowTitle(self.tr("提示词中心"))
        self.titleLabel = BodyLabel(self.tr("提示词中心"), self)
        self.promptCombo = ComboBox(self)
        self.promptCombo.addItems([item["title"] for item in self.prompt_items])
        self.descriptionLabel = BodyLabel("", self)
        self.descriptionLabel.setWordWrap(True)
        self.variableLabel = BodyLabel("", self)
        self.variableLabel.setWordWrap(True)

        self.textEdit = TextEdit(self)
        self.textEdit.setMinimumSize(660, self._editor_height)
        self.textEdit.setMaximumHeight(self._editor_height)
        self.textEdit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.saveButton = PushButton(self.tr("保存当前提示词"), self.buttonGroup)
        self.importButton = PushButton(self.tr("导入术语文件"), self.buttonGroup)
        self.restoreButton = PushButton(self.tr("恢复默认"), self.buttonGroup)
        self.defaultButton = PushButton(self.tr("查看默认"), self.buttonGroup)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.promptCombo)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addWidget(self.variableLabel)
        self.viewLayout.addWidget(self.textEdit)
        self.viewLayout.setSpacing(10)

        self.yesButton.setText(self.tr("关闭"))
        self.cancelButton.hide()
        self.buttonLayout.insertWidget(0, self.saveButton, 0, Qt.AlignVCenter)
        self.buttonLayout.insertWidget(1, self.importButton, 0, Qt.AlignVCenter)
        self.buttonLayout.insertWidget(2, self.restoreButton, 0, Qt.AlignVCenter)
        self.buttonLayout.insertWidget(3, self.defaultButton, 0, Qt.AlignVCenter)
        self.buttonLayout.insertStretch(4, 1)

        self.promptCombo.currentIndexChanged.connect(self.on_prompt_changed)
        self.saveButton.clicked.connect(self.save_current_prompt)
        self.importButton.clicked.connect(self.import_glossary_file)
        self.restoreButton.clicked.connect(self.restore_current_prompt)
        self.defaultButton.clicked.connect(self.show_default_prompt)

    def _current_item(self) -> dict:
        index = self.promptCombo.currentIndex()
        if index < 0:
            index = 0
        return self.prompt_items[index]

    def on_prompt_changed(self, _=None):
        self.current_prompt_id = self._current_item()["id"]
        self.load_prompt(self.current_prompt_id)

    def load_prompt(self, prompt_id: str):
        item = self._current_item()
        self.descriptionLabel.setText(item["description"])
        required_variables = sorted(get_required_prompt_variables(prompt_id))
        if required_variables:
            self.variableLabel.setText(
                self.tr("必需变量：") + ", ".join("${" + v + "}" for v in required_variables)
            )
        else:
            self.variableLabel.setText(self.tr("必需变量：无"))
        self.textEdit.setPlainText(get_prompt_template(prompt_id))
        self.importButton.setVisible(prompt_id == PROMPT_TERM_GLOSSARY)

    def import_glossary_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("导入术语文件"),
            "",
            self.tr("Markdown / 文本文件 (*.md *.txt);;所有文件 (*)"),
        )
        if not file_path:
            return

        try:
            content = Path(file_path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            InfoBar.error(
                self.tr("导入失败"),
                self.tr("无法读取文件：") + str(exc),
                duration=5000,
                parent=self,
            )
            return

        pairs = extract_glossary_pairs(content)
        if not pairs:
            InfoBar.warning(
                self.tr("未找到术语"),
                self.tr("文件中没有可识别的“原文 -> 译文”对照项"),
                duration=5000,
                parent=self,
            )
            return

        self.textEdit.setPlainText(format_glossary_pairs(pairs))
        InfoBar.success(
            self.tr("导入完成"),
            self.tr("已提取 {0} 条术语，请检查后保存").format(len(pairs)),
            duration=4000,
            parent=self,
        )

    def _config_item(self, prompt_id: str):
        return getattr(cfg, get_prompt_config_attr(prompt_id))

    def save_current_prompt(self):
        prompt_id = self.current_prompt_id
        prompt_text = self.textEdit.toPlainText()
        missing_variables = validate_prompt_template(prompt_id, prompt_text)
        if prompt_text.strip() and missing_variables:
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("缺少必需变量：")
                + ", ".join("${" + v + "}" for v in missing_variables),
                duration=5000,
                parent=self,
            )
            return

        cfg.set(self._config_item(prompt_id), prompt_text)
        InfoBar.success(
            self.tr("保存成功"),
            self.tr("提示词已更新"),
            duration=2500,
            parent=self,
        )

    def restore_current_prompt(self):
        prompt_id = self.current_prompt_id
        cfg.set(self._config_item(prompt_id), "")
        self.load_prompt(prompt_id)
        InfoBar.success(
            self.tr("已恢复默认"),
            self.tr("当前提示词将使用内置默认值"),
            duration=2500,
            parent=self,
        )

    def show_default_prompt(self):
        item = self._current_item()
        dialog = DefaultPromptDialog(
            self.tr(item["title"] + " - 默认内容"),
            get_default_prompt_template(self.current_prompt_id),
            self,
        )
        dialog.exec_()


class DescriptionTemplateDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        max_width, edit_height = DefaultPromptDialog._dialog_dimensions(parent)
        self.widget.setMaximumWidth(max_width)
        self.widget.setMinimumWidth(min(max_width, 720))
        self.setWindowTitle(self.tr("视频信息文本模板"))

        self.titleLabel = BodyLabel(self.tr("视频信息文本模板"), self)
        self.descriptionLabel = BodyLabel(
            self.tr("下载视频时，使用此模板生成与视频同名的 .txt 文件。"), self
        )
        self.descriptionLabel.setWordWrap(True)
        variable_text = "  ".join(
            f"${{{name}}}：{description}"
            for name, description in DESCRIPTION_TEMPLATE_VARIABLES.items()
        )
        self.variableLabel = BodyLabel(self.tr("可用变量：") + variable_text, self)
        self.variableLabel.setWordWrap(True)

        self.textEdit = TextEdit(self)
        self.textEdit.setPlainText(self._current_template())
        self.textEdit.setMinimumSize(min(max_width - 80, 680), edit_height)
        self.textEdit.setMaximumHeight(edit_height)
        self.textEdit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.saveButton = PushButton(self.tr("保存模板"), self.buttonGroup)
        self.restoreButton = PushButton(self.tr("恢复默认"), self.buttonGroup)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addWidget(self.variableLabel)
        self.viewLayout.addWidget(self.textEdit)
        self.viewLayout.setSpacing(10)

        self.yesButton.setText(self.tr("关闭"))
        self.cancelButton.hide()
        self.buttonLayout.insertWidget(0, self.saveButton, 0, Qt.AlignVCenter)
        self.buttonLayout.insertWidget(1, self.restoreButton, 0, Qt.AlignVCenter)
        self.buttonLayout.insertStretch(2, 1)

        self.saveButton.clicked.connect(self.save_template)
        self.restoreButton.clicked.connect(self.restore_default)

    @staticmethod
    def _current_template() -> str:
        template = str(cfg.get(cfg.download_description_txt_template) or "")
        return template if template.strip() else DEFAULT_DESCRIPTION_TEMPLATE

    def save_template(self):
        template = self.textEdit.toPlainText()
        if not template.strip():
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("模板不能为空；如需重置，请使用“恢复默认”。"),
                duration=4000,
                parent=self,
            )
            return

        unknown_variables = find_unknown_template_variables(template)
        if unknown_variables:
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("包含未知变量：")
                + ", ".join(f"${{{name}}}" for name in unknown_variables),
                duration=5000,
                parent=self,
            )
            return

        cfg.set(cfg.download_description_txt_template, template)
        InfoBar.success(
            self.tr("保存成功"),
            self.tr("后续下载生成的视频信息文本将使用此模板。"),
            duration=3000,
            parent=self,
        )

    def restore_default(self):
        cfg.set(cfg.download_description_txt_template, "")
        self.textEdit.setPlainText(DEFAULT_DESCRIPTION_TEMPLATE)
        InfoBar.success(
            self.tr("已恢复默认"),
            self.tr("视频信息文本将使用内置默认模板。"),
            duration=2500,
            parent=self,
        )


class SettingInterface(ScrollArea):
    """设置界面"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._cookie_export_in_progress = False
        self.setWindowTitle(self.tr("设置"))
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)
        self.settingLabel = QLabel(self.tr("设置"), self)

        # 初始化所有设置组
        self.__initGroups()
        # 初始化所有配置卡片
        self.__initCards()
        # 初始化界面
        self.__initWidget()
        # 初始化布局
        self.__initLayout()
        # 连接信号和槽
        self.__connectSignalToSlot()
        cfg.themeMode.valueChanged.connect(lambda *_: self.__applyPageStyles())

    def showEvent(self, event):
        super().showEvent(event)
        self.__applyPageStyles()
        self.__refreshDownloadProxyStatus()
        self.__refreshDownloadCenterOutputDir()
        self.refresh_cookie_status()
        self.__refreshDesktopNotificationStatus()

    def __initGroups(self):
        """初始化所有设置组"""
        # 转录配置组
        self.transcribeGroup = SettingCardGroup(self.tr("转录配置"), self.scrollWidget)
        # LLM配置组
        self.llmGroup = SettingCardGroup(self.tr("LLM配置"), self.scrollWidget)
        # 翻译服务组
        self.translate_serviceGroup = SettingCardGroup(
            self.tr("翻译服务"), self.scrollWidget
        )
        # 翻译与优化组
        self.translateGroup = SettingCardGroup(self.tr("翻译与优化"), self.scrollWidget)
        # 提示词中心组
        self.promptCenterGroup = SettingCardGroup(self.tr("提示词中心"), self.scrollWidget)
        # 字幕输出配置组
        self.subtitleGroup = SettingCardGroup(
            self.tr("字幕输出配置"), self.scrollWidget
        )
        # 保存配置组
        self.saveGroup = SettingCardGroup(self.tr("保存配置"), self.scrollWidget)
        # 下载网络组
        self.downloadGroup = SettingCardGroup(self.tr("下载网络"), self.scrollWidget)
        # 下载设置组
        self.downloadSettingGroup = SettingCardGroup(
            self.tr("下载设置"), self.scrollWidget
        )
        # 账号验证组
        self.downloadAccountGroup = SettingCardGroup(
            self.tr("账号验证"), self.scrollWidget
        )
        # 个性化组
        self.personalGroup = SettingCardGroup(self.tr("个性化"), self.scrollWidget)
        # 关于组
        self.aboutGroup = SettingCardGroup(self.tr("关于"), self.scrollWidget)

    def __initCards(self):
        """初始化所有配置卡片"""
        # 转录配置卡片
        self.transcribeModelCard = ComboBoxSettingCard(
            cfg.transcribe_model,
            FIF.MICROPHONE,
            self.tr("转录模型"),
            self.tr("语音转换文字要使用的语音识别模型"),
            texts=[model.value for model in cfg.transcribe_model.validator.options],
            parent=self.transcribeGroup,
        )

        self.modelDownloadCard = PushSettingCard(
            self.tr("查看模型"),
            FIF.DOWNLOAD,
            self.tr("常用模型下载"),
            self.tr("浏览 MLX Whisper、WhisperX 常用模型，跳转下载页面"),
            self.transcribeGroup,
        )
        self.modelDownloadCard.clicked.connect(self.__showModelDownloadDialog)

        # LLM配置卡片
        self.__createLLMServiceCards()

        # 翻译配置卡片
        self.__createTranslateServiceCards()

        # 翻译与优化配置卡片
        self.subtitleCorrectCard = SwitchSettingCard(
            FIF.EDIT,
            self.tr("字幕校正"),
            self.tr("字幕处理过程是否对生成的字幕进行校正"),
            cfg.need_optimize,
            self.translateGroup,
        )
        self.subtitleTranslateCard = SwitchSettingCard(
            FIF.LANGUAGE,
            self.tr("字幕翻译"),
            self.tr("字幕处理过程是否对生成的字幕进行翻译"),
            cfg.need_translate,
            self.translateGroup,
        )
        self.targetLanguageCard = ComboBoxSettingCard(
            cfg.target_language,
            FIF.LANGUAGE,
            self.tr("目标语言"),
            self.tr("选择翻译字幕的目标语言"),
            texts=[lang.value for lang in cfg.target_language.validator.options],
            parent=self.translateGroup,
        )
        self.promptCenterCard = PushSettingCard(
            self.tr("打开"),
            FIF.DOCUMENT,
            self.tr("提示词中心"),
            self.tr("统一编辑断句、校正、翻译、反思翻译和转录提示词"),
            self.promptCenterGroup,
        )

        self.subtitleLayoutCard = ComboBoxSettingCard(
            cfg.subtitle_layout,
            FIF.ALIGNMENT,
            self.tr("字幕布局"),
            self.tr("选择字幕的布局（单语、双语）"),
            texts=[layout for layout in cfg.subtitle_layout.validator.options],
            parent=self.subtitleGroup,
        )
        # 保存配置卡片
        self.savePathCard = PushSettingCard(
            self.tr("工作文件夹"),
            FIF.SAVE,
            self.tr("工作目录路径"),
            cfg.get(cfg.work_dir),
            self.saveGroup,
        )

        self.downloadProxyModeCard = ComboBoxSettingCard(
            cfg.download_proxy_mode,
            FIF.SEARCH,
            self.tr("下载代理模式"),
            self.tr("自动检测本机代理，或手动指定代理地址"),
            texts=["自动检测", "手动设置", "不使用代理"],
            parent=self.downloadGroup,
        )
        self.downloadProxyUrlCard = LineEditSettingCard(
            cfg.download_proxy_url,
            FIF.LINK,
            self.tr("手动代理地址"),
            self.tr("支持 http://127.0.0.1:7897 这类地址"),
            "http://127.0.0.1:7897",
            self.downloadGroup,
        )
        self.downloadProxyStatusCard = PushSettingCard(
            self.tr("刷新"),
            FIF.LINK,
            self.tr("当前生效代理"),
            self.tr("检测中"),
            self.downloadGroup,
        )
        self.downloadEngineStrategyCard = ComboBoxSettingCard(
            cfg.download_engine_strategy,
            FIF.SPEED_HIGH,
            self.tr("下载引擎策略"),
            self.tr("仅作用于下载中心，用于控制 yt-dlp 的单线程、多线程或智能选择"),
            texts=["单线程", "多线程", "智能选择"],
            parent=self.downloadSettingGroup,
        )
        self.downloadHevcEncoderCard = ComboBoxSettingCard(
            cfg.download_hevc_encoder,
            FIF.VIDEO,
            self.tr("H.265 转码编码器"),
            self.tr(
                "自动优先使用硬件编码，失败后使用 CPU；指定编码器不可用时会报错"
            ),
            texts=["自动", "NVIDIA NVENC", "Intel QSV", "AMD AMF", "CPU（libx265）"],
            parent=self.downloadSettingGroup,
        )
        self.downloadAutoExtractCookiesOnStartupCard = SwitchSettingCard(
            FIF.SYNC,
            self.tr("应用启动时自动提取浏览器 Cookie"),
            self.tr("下次启动应用时，自动从所选浏览器提取并更新 cookies.txt"),
            cfg.download_auto_extract_cookies_on_startup,
            self.downloadSettingGroup,
        )
        self.downloadCookieBrowserCard = ComboBoxSettingCard(
            cfg.download_cookie_browser,
            FIF.GLOBE,
            self.tr("Cookie 来源浏览器"),
            self.tr(
                "提取 cookies.txt 时只读取所选浏览器；请先在该浏览器登录目标网站"
            ),
            texts=cfg.download_cookie_browser.options,
            parent=self.downloadSettingGroup,
        )
        self.downloadCenterOutputDirCard = PushSettingCard(
            self.tr("查看"),
            FIF.FOLDER,
            self.tr("下载中心默认输出目录"),
            self.tr("跟随工作目录"),
            self.downloadSettingGroup,
        )
        self.downloadDescriptionTemplateCard = PushSettingCard(
            self.tr("编辑"),
            FIF.DOCUMENT,
            self.tr("视频信息文本模板"),
            self.tr("编辑下载时生成的同名 .txt 文件内容和动态变量"),
            self.downloadSettingGroup,
        )
        self.edgeCookieExportCard = PrimaryPushSettingCard(
            self.tr("提取"),
            FIF.DOWNLOAD,
            self.tr("从浏览器提取 Cookie"),
            self.tr("导出到应用支持目录，供 YouTube 下载链路使用"),
            self.downloadAccountGroup,
        )
        self.edgeCookieStatusCard = PushSettingCard(
            self.tr("验证"),
            FIF.INFO,
            self.tr("Cookie 状态"),
            self.tr("未检测"),
            self.downloadAccountGroup,
        )

        # 个性化配置卡片
        self.themeCard = OptionsSettingCard(
            cfg.themeMode,
            FIF.BRUSH,
            self.tr("应用主题"),
            self.tr("更改应用程序的外观"),
            texts=[self.tr("浅色"), self.tr("深色"), self.tr("使用系统设置")],
            parent=self.personalGroup,
        )
        self.themeColorCard = CustomColorSettingCard(
            cfg.themeColor,
            FIF.PALETTE,
            self.tr("主题颜色"),
            self.tr("更改应用程序的主题颜色"),
            self.personalGroup,
        )
        self.zoomCard = OptionsSettingCard(
            cfg.dpiScale,
            FIF.ZOOM,
            self.tr("界面缩放"),
            self.tr("更改小部件和字体的大小"),
            texts=["100%", "125%", "150%", "175%", "200%", self.tr("使用系统设置")],
            parent=self.personalGroup,
        )
        self.languageCard = ComboBoxSettingCard(
            cfg.language,
            FIF.LANGUAGE,
            self.tr("语言"),
            self.tr("设置您偏好的界面语言"),
            texts=["简体中文", "繁體中文", "English", self.tr("使用系统设置")],
            parent=self.personalGroup,
        )
        self.desktopNotificationsCard = SwitchSettingCard(
            FIF.RINGER,
            self.tr("桌面通知"),
            self.tr("长任务完成或失败时发送系统通知"),
            cfg.desktop_notifications_enabled,
            self.personalGroup,
        )
        self.desktopNotificationStatusCard = PushSettingCard(
            self.tr("刷新"),
            FIF.INFO,
            self.tr("通知权限状态"),
            self.tr("未检测"),
            self.personalGroup,
        )
        self.desktopNotificationTestCard = PrimaryPushSettingCard(
            self.tr("发送"),
            FIF.SEND,
            self.tr("测试通知"),
            self.tr("发送一条系统通知，用于确认权限和点击跳转是否正常"),
            self.personalGroup,
        )

        # 关于卡片
        self.helpCard = HyperlinkCard(
            HELP_URL,
            self.tr("打开帮助页面"),
            FIF.HELP,
            self.tr("帮助"),
            self.tr("发现新功能并了解有关VideoCaptioner的使用技巧"),
            self.aboutGroup,
        )
        self.feedbackCard = PrimaryPushSettingCard(
            self.tr("提供反馈"),
            FIF.FEEDBACK,
            self.tr("提供反馈"),
            self.tr("提供反馈帮助我们改进VideoCaptioner"),
            self.aboutGroup,
        )
        self.aboutCard = PrimaryPushSettingCard(
            self.tr("查看"),
            FIF.INFO,
            self.tr("关于"),
            "© "
            + self.tr("版权所有")
            + f" {YEAR}, {AUTHOR}.",
            self.aboutGroup,
        )

        # 添加卡片到对应的组
        self.translateGroup.addSettingCard(self.subtitleCorrectCard)
        self.translateGroup.addSettingCard(self.subtitleTranslateCard)
        self.translateGroup.addSettingCard(self.targetLanguageCard)
        self.promptCenterGroup.addSettingCard(self.promptCenterCard)

        self.subtitleGroup.addSettingCard(self.subtitleLayoutCard)

        self.saveGroup.addSettingCard(self.savePathCard)
        self.downloadSettingGroup.addSettingCard(self.downloadEngineStrategyCard)
        self.downloadSettingGroup.addSettingCard(self.downloadHevcEncoderCard)
        self.downloadSettingGroup.addSettingCard(
            self.downloadAutoExtractCookiesOnStartupCard
        )
        self.downloadSettingGroup.addSettingCard(self.downloadCookieBrowserCard)
        self.downloadSettingGroup.addSettingCard(self.downloadCenterOutputDirCard)
        self.downloadSettingGroup.addSettingCard(
            self.downloadDescriptionTemplateCard
        )
        self.downloadAccountGroup.addSettingCard(self.edgeCookieExportCard)
        self.downloadAccountGroup.addSettingCard(self.edgeCookieStatusCard)

        self.personalGroup.addSettingCard(self.themeCard)
        self.personalGroup.addSettingCard(self.themeColorCard)
        self.personalGroup.addSettingCard(self.zoomCard)
        self.personalGroup.addSettingCard(self.languageCard)
        self.personalGroup.addSettingCard(self.desktopNotificationsCard)
        self.personalGroup.addSettingCard(self.desktopNotificationStatusCard)
        self.personalGroup.addSettingCard(self.desktopNotificationTestCard)

        self.aboutGroup.addSettingCard(self.helpCard)
        self.aboutGroup.addSettingCard(self.feedbackCard)
        self.aboutGroup.addSettingCard(self.aboutCard)

    def __createLLMServiceCards(self):
        """创建LLM服务相关的配置卡片"""
        # 服务选择卡片
        self.llmServiceCard = ComboBoxSettingCard(
            cfg.llm_service,
            FIF.ROBOT,
            self.tr("LLM服务"),
            self.tr("选择用于字幕断句、字幕优化、字幕翻译的大模型服务"),
            texts=[service.value for service in cfg.llm_service.validator.options],
            parent=self.llmGroup,
        )
        self.llmTimeoutCard = SpinBoxSettingCard(
            cfg.llm_request_timeout,
            FIF.SPEED_HIGH,
            self.tr("请求超时"),
            self.tr("LLM API 请求等待时间（秒）"),
            minimum=30,
            maximum=900,
            parent=self.llmGroup,
        )
        self.llmCacheEnabledCard = SwitchSettingCard(
            FIF.HISTORY,
            self.tr("启用 API 结果缓存"),
            self.tr("相同字幕、模型和提示词配置会复用结果，减少重复请求和 token 消耗"),
            cfg.llm_cache_enabled,
            self.llmGroup,
        )
        self.llmBatchContextEnabledCard = SwitchSettingCard(
            FIF.CHAT,
            self.tr("启用批次上下文"),
            self.tr("每批请求附带上一批字幕上下文，提高术语一致性但会增加 token"),
            cfg.llm_batch_context_enabled,
            self.llmGroup,
        )
        self.llmBatchContextMaxCharsCard = SpinBoxSettingCard(
            cfg.llm_batch_context_max_chars,
            FIF.ALIGNMENT,
            self.tr("批次上下文最大字符数"),
            self.tr("0 表示不附带上下文；数值越大一致性越好但 token 消耗越高"),
            minimum=0,
            maximum=1000,
            parent=self.llmGroup,
        )

        # 创建OPENAI官方API链接卡片
        self.openaiOfficialApiCard = HyperlinkCard(
            "https://api.videocaptioner.cn/register?aff=UrLB",
            self.tr("访问"),
            FIF.DEVELOPER_TOOLS,
            self.tr("VideoCaptioner 官方API"),
            self.tr("集成多种大语言模型，支持高并发字幕优化、翻译"),
            self.llmGroup,
        )
        # 默认隐藏
        self.openaiOfficialApiCard.setVisible(False)

        # 定义每个服务的配置
        service_configs = {
            LLMServiceEnum.OPENAI: {
                "prefix": "openai",
                "api_key_cfg": cfg.openai_api_key,
                "api_base_cfg": cfg.openai_api_base,
                "model_cfg": cfg.openai_model,
                "default_base": "https://api.openai.com/v1",
                "default_models": [
                    "gpt-4o-mini",
                    "gpt-4o",
                    "claude-3-5-sonnet-20241022",
                ],
            },
            LLMServiceEnum.SILICON_CLOUD: {
                "prefix": "silicon_cloud",
                "api_key_cfg": cfg.silicon_cloud_api_key,
                "api_base_cfg": cfg.silicon_cloud_api_base,
                "model_cfg": cfg.silicon_cloud_model,
                "default_base": "https://api.siliconflow.cn/v1",
                "default_models": ["deepseek-ai/DeepSeek-V3"],
            },
            LLMServiceEnum.DEEPSEEK: {
                "prefix": "deepseek",
                "api_key_cfg": cfg.deepseek_api_key,
                "api_base_cfg": cfg.deepseek_api_base,
                "model_cfg": cfg.deepseek_model,
                "default_base": "https://api.deepseek.com/v1",
                "default_models": ["v4-pro", "v4-flash"],
            },
            LLMServiceEnum.OLLAMA: {
                "prefix": "ollama",
                "api_key_cfg": cfg.ollama_api_key,
                "api_base_cfg": cfg.ollama_api_base,
                "model_cfg": cfg.ollama_model,
                "default_base": "http://localhost:11434/v1",
                "default_models": ["qwen2.5:7b"],
            },
            LLMServiceEnum.LM_STUDIO: {
                "prefix": "LM Studio",
                "api_key_cfg": cfg.lm_studio_api_key,
                "api_base_cfg": cfg.lm_studio_api_base,
                "model_cfg": cfg.lm_studio_model,
                "default_base": "http://localhost:1234/v1",
                "default_models": ["qwen2.5:7b"],
            },
            LLMServiceEnum.GEMINI: {
                "prefix": "gemini",
                "api_key_cfg": cfg.gemini_api_key,
                "api_base_cfg": cfg.gemini_api_base,
                "model_cfg": cfg.gemini_model,
                "default_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "default_models": ["gemini-2.0-flash-exp"],
            },
            LLMServiceEnum.CHATGLM: {
                "prefix": "chatglm",
                "api_key_cfg": cfg.chatglm_api_key,
                "api_base_cfg": cfg.chatglm_api_base,
                "model_cfg": cfg.chatglm_model,
                "default_base": "https://open.bigmodel.cn/api/paas/v4",
                "default_models": ["glm-4-flash"],
            },
            LLMServiceEnum.QWEN: {
                "prefix": "qwen",
                "api_key_cfg": cfg.qwen_api_key,
                "api_base_cfg": cfg.qwen_api_base,
                "model_cfg": cfg.qwen_model,
                "default_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "default_models": ["qwen-plus", "qwen-turbo", "qwen-max"],
            },
            LLMServiceEnum.PUBLIC: {
                "prefix": "public",
                "api_key_cfg": cfg.public_api_key,
                "api_base_cfg": cfg.public_api_base,
                "model_cfg": cfg.public_model,
                "default_base": "https://api.public-model.com/v1",
                "default_models": ["public-model"],
            },
        }

        # 创建服务配置映射
        self.llm_service_configs = {}

        # 为每个服务创建配置卡片
        for service, config in service_configs.items():
            prefix = config["prefix"]

            # 如果是公益模型，只添加配置不创建卡片
            if service == LLMServiceEnum.PUBLIC:
                self.llm_service_configs[service] = {
                    "cards": [],
                    "api_base": None,
                    "api_key": None,
                    "model": None,
                }
                continue

            # 创建API Key卡片
            api_key_card = LineEditSettingCard(
                config["api_key_cfg"],
                FIF.FINGERPRINT,
                self.tr("API Key"),
                self.tr(f"输入您的 {service.value} API Key"),
                "sk-" if service != LLMServiceEnum.OLLAMA else "",
                self.llmGroup,
            )
            setattr(self, f"{prefix}_api_key_card", api_key_card)

            # 创建Base URL卡片
            api_base_card = LineEditSettingCard(
                config["api_base_cfg"],
                FIF.LINK,
                self.tr("Base URL"),
                self.tr(f"输入 {service.value} Base URL, 需要包含 /v1"),
                config["default_base"],
                self.llmGroup,
            )
            setattr(self, f"{prefix}_api_base_card", api_base_card)

            # 创建模型选择卡片
            model_card = EditComboBoxSettingCard(
                config["model_cfg"],
                FIF.ROBOT,
                self.tr("模型"),
                self.tr(f"选择 {service.value} 模型"),
                config["default_models"],
                self.llmGroup,
            )
            setattr(self, f"{prefix}_model_card", model_card)

            qwen_thinking_card = None
            if service == LLMServiceEnum.QWEN:
                qwen_thinking_card = SwitchSettingCard(
                    FIF.HISTORY,
                    self.tr("启用思考模式"),
                    self.tr("仅对 Qwen 服务生效；开启后会在请求体中启用思考模式"),
                    cfg.qwen_enable_thinking,
                    self.llmGroup,
                )
                setattr(self, f"{prefix}_thinking_card", qwen_thinking_card)

            # 存储服务配置
            cards = [api_key_card, api_base_card, model_card]
            if qwen_thinking_card:
                cards.append(qwen_thinking_card)

            self.llm_service_configs[service] = {
                "cards": cards,
                "api_base": api_base_card,
                "api_key": api_key_card,
                "model": model_card,
                "qwen_thinking": qwen_thinking_card,
            }

        # 创建检查连接卡片
        self.checkLLMConnectionCard = PushSettingCard(
            self.tr("检查连接"),
            FIF.LINK,
            self.tr("检查 LLM 连接"),
            self.tr("点击检查 API 连接是否正常，并获取模型列表"),
            self.llmGroup,
        )

        # 初始化显示状态
        self.__onLLMServiceChanged(self.llmServiceCard.comboBox.currentText())

    def __createTranslateServiceCards(self):
        """创建 LLM 翻译配置卡片"""
        # 反思翻译开关
        self.needReflectTranslateCard = SwitchSettingCard(
            FIF.EDIT,
            self.tr("需要反思翻译"),
            self.tr("启用反思翻译可以提高翻译质量，但耗费更多时间和token"),
            cfg.need_reflect_translate,
            self.translate_serviceGroup,
        )

        # 批处理大小配置
        self.batchSizeCard = RangeSettingCard(
            cfg.batch_size,
            FIF.ALIGNMENT,
            self.tr("每次请求字幕条数"),
            self.tr("每次 API 请求包含的字幕条数；越大请求次数越少，但单次 token 更多"),
            parent=self.translate_serviceGroup,
        )

        self.translationMaxLengthCard = SpinBoxSettingCard(
            cfg.translation_max_length,
            FIF.SPEED_HIGH,
            self.tr("译文长度建议"),
            self.tr(
                "0 表示不限制；系统会结合字幕时长给出阅读建议，完整准确优先"
            ),
            minimum=0,
            maximum=80,
            parent=self.translate_serviceGroup,
        )

        self.finalTranslationReworkMaxCharsCard = SpinBoxSettingCard(
            cfg.final_translation_rework_max_chars,
            FIF.SPEED_HIGH,
            self.tr("最终译文自动回炉字数"),
            self.tr("0 表示关闭；最终译文超过该字数时只用原文重新翻译一次"),
            minimum=0,
            maximum=120,
            parent=self.translate_serviceGroup,
        )

        # 线程数配置
        self.threadNumCard = RangeSettingCard(
            cfg.thread_num,
            FIF.SPEED_HIGH,
            self.tr("API 并发数"),
            self.tr(
                "同时发送的 API 请求数量；实际并发不会超过当前任务批次数"
            ),
            parent=self.translate_serviceGroup,
        )

        # 添加卡片到翻译服务组
        self.translate_serviceGroup.addSettingCard(self.needReflectTranslateCard)
        self.translate_serviceGroup.addSettingCard(self.batchSizeCard)
        self.translate_serviceGroup.addSettingCard(self.translationMaxLengthCard)
        self.translate_serviceGroup.addSettingCard(self.finalTranslationReworkMaxCharsCard)
        self.translate_serviceGroup.addSettingCard(self.threadNumCard)


    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("settingInterface")

        # 初始化样式表
        self.scrollWidget.setObjectName("scrollWidget")
        self.settingLabel.setObjectName("settingLabel")

        self.__onLLMBatchContextChanged(cfg.llm_batch_context_enabled.value)
        self.__applyPageStyles()
        self.__refreshDownloadCenterOutputDir()
        self.refresh_cookie_status()

    def __initLayout(self):
        """初始化布局"""
        self.settingLabel.move(36, 30)

        # 添加转录配置卡片
        self.transcribeGroup.addSettingCard(self.transcribeModelCard)
        self.transcribeGroup.addSettingCard(self.modelDownloadCard)

        # 添加LLM配置卡片
        self.llmGroup.addSettingCard(self.llmServiceCard)
        self.llmGroup.addSettingCard(self.llmTimeoutCard)
        self.llmGroup.addSettingCard(self.llmCacheEnabledCard)
        self.llmGroup.addSettingCard(self.llmBatchContextEnabledCard)
        self.llmGroup.addSettingCard(self.llmBatchContextMaxCharsCard)
        # 添加OPENAI官方API链接卡片
        self.llmGroup.addSettingCard(self.openaiOfficialApiCard)
        for config in self.llm_service_configs.values():
            for card in config["cards"]:
                self.llmGroup.addSettingCard(card)
        self.llmGroup.addSettingCard(self.checkLLMConnectionCard)

        self.downloadGroup.addSettingCard(self.downloadProxyModeCard)
        self.downloadGroup.addSettingCard(self.downloadProxyUrlCard)
        self.downloadGroup.addSettingCard(self.downloadProxyStatusCard)

        # 将所有组添加到布局
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        self.expandLayout.addWidget(self.transcribeGroup)
        self.expandLayout.addWidget(self.llmGroup)
        self.expandLayout.addWidget(self.translate_serviceGroup)
        self.expandLayout.addWidget(self.translateGroup)
        self.expandLayout.addWidget(self.promptCenterGroup)
        self.expandLayout.addWidget(self.subtitleGroup)
        self.expandLayout.addWidget(self.saveGroup)
        self.expandLayout.addWidget(self.downloadGroup)
        self.expandLayout.addWidget(self.downloadSettingGroup)
        self.expandLayout.addWidget(self.downloadAccountGroup)
        self.expandLayout.addWidget(self.personalGroup)
        self.expandLayout.addWidget(self.aboutGroup)

    def __connectSignalToSlot(self):
        """连接信号与槽"""
        cfg.appRestartSig.connect(self.__showRestartTooltip)

        # LLM服务切换
        self.llmServiceCard.comboBox.currentTextChanged.connect(
            self.__onLLMServiceChanged
        )

        # 检查 LLM 连接
        self.checkLLMConnectionCard.clicked.connect(self.checkLLMConnection)
        self.promptCenterCard.clicked.connect(self.__showPromptCenterDialog)
        self.downloadDescriptionTemplateCard.clicked.connect(
            self.__showDescriptionTemplateDialog
        )

        # 保存路径
        self.savePathCard.clicked.connect(self.__onsavePathCardClicked)
        self.downloadProxyStatusCard.clicked.connect(self.__refreshDownloadProxyStatus)
        self.downloadProxyModeCard.comboBox.currentTextChanged.connect(
            self.__onDownloadProxyModeChanged
        )
        self.downloadProxyUrlCard.textChanged.connect(
            lambda _: self.__refreshDownloadProxyStatus()
        )
        self.llmBatchContextEnabledCard.checkedChanged.connect(
            self.__onLLMBatchContextChanged
        )
        self.downloadCenterOutputDirCard.clicked.connect(
            self.__showDownloadCenterOutputDir
        )
        self.downloadCookieBrowserCard.comboBox.currentTextChanged.connect(
            lambda _: self.refresh_cookie_status()
        )
        self.edgeCookieExportCard.clicked.connect(self.__exportEdgeCookies)
        self.edgeCookieStatusCard.clicked.connect(
            lambda: self.refresh_cookie_status()
        )

        # 个性化
        self.themeCard.optionChanged.connect(self.__onThemeCardChanged)
        self.themeColorCard.colorChanged.connect(setThemeColor)
        self.desktopNotificationsCard.checkedChanged.connect(
            self.__onDesktopNotificationsChanged
        )
        self.desktopNotificationStatusCard.clicked.connect(
            self.__refreshDesktopNotificationStatus
        )
        self.desktopNotificationTestCard.clicked.connect(
            self.__sendTestDesktopNotification
        )

        # 反馈
        self.feedbackCard.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(FEEDBACK_URL))
        )

        # 关于
        self.aboutCard.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(HELP_URL))
        )

        # 全局 signalBus
        self.transcribeModelCard.comboBox.currentTextChanged.connect(
            signalBus.transcription_model_changed
        )
        self.subtitleCorrectCard.checkedChanged.connect(
            signalBus.subtitle_optimization_changed
        )
        self.subtitleTranslateCard.checkedChanged.connect(
            signalBus.subtitle_translation_changed
        )
        self.targetLanguageCard.comboBox.currentTextChanged.connect(
            signalBus.target_language_changed
        )
        self.__onDownloadProxyModeChanged(
            self.downloadProxyModeCard.comboBox.currentText()
        )

    def __onThemeCardChanged(self, config_item):
        setTheme(cfg.get(config_item))
        self.__applyPageStyles()

    def __showModelDownloadDialog(self):
        dialog = ModelDownloadDialog(self.window(), cfg.transcribe_model.value)
        dialog.exec_()

    def __showPromptCenterDialog(self):
        dialog = PromptCenterDialog(self)
        dialog.exec_()

    def __showDescriptionTemplateDialog(self):
        dialog = DescriptionTemplateDialog(self)
        dialog.exec_()

    def __onLLMBatchContextChanged(self, checked: bool):
        self.llmBatchContextMaxCharsCard.setEnabled(bool(checked))

    def __applyPageStyles(self):
        if isDarkTheme():
            page_background = "#202124"
            label_color = "#F5F5F5"
            group_title_color = "#F1F2F4"
            card_background = "rgba(255, 255, 255, 0.05)"
            card_border = "rgba(255, 255, 255, 0.08)"
        else:
            page_background = "#F5F7FA"
            label_color = "#1D2939"
            group_title_color = "#344054"
            card_background = "#FFFFFF"
            card_border = "rgba(17, 24, 39, 0.10)"

        self.setStyleSheet(
            f"""        
            SettingInterface, #scrollWidget {{
                background-color: {page_background};
            }}
            QScrollArea {{
                border: none;
                background-color: {page_background};
            }}
            QLabel#settingLabel {{
                font: 33px 'Microsoft YaHei';
                background-color: transparent;
                color: {label_color};
            }}
            SettingCardGroup > QLabel {{
                color: {group_title_color};
                font-weight: 600;
            }}
            SettingCard, SwitchSettingCard, PushSettingCard,
            PrimaryPushSettingCard, ComboBoxSettingCard, CustomColorSettingCard,
            OptionsSettingCard, RangeSettingCard, HyperlinkCard,
            LineEditSettingCard, SpinBoxSettingCard, EditComboBoxSettingCard {{
                background-color: {card_background};
                border: 1px solid {card_border};
                border-radius: 8px;
            }}
        """
        )

    def __refreshDownloadCenterOutputDir(self):
        output_dir = str(cfg.get(cfg.download_center_output_dir) or "").strip()
        if not output_dir:
            output_dir = self.tr("跟随工作目录")
        self.downloadCenterOutputDirCard.setContent(output_dir)

    def __onDesktopNotificationsChanged(self, checked: bool):
        if checked:
            request_desktop_notification_authorization()
        self.__refreshDesktopNotificationStatus()

    def __refreshDesktopNotificationStatus(self):
        status = get_desktop_notification_status()
        self.desktopNotificationStatusCard.setContent(
            status.get("message") or self.tr("未知")
        )

    def __sendTestDesktopNotification(self):
        if not cfg.get(cfg.desktop_notifications_enabled):
            InfoBar.warning(
                self.tr("通知未开启"),
                self.tr("请先打开桌面通知开关。"),
                duration=3000,
                parent=self,
            )
            return

        request_desktop_notification_authorization()
        sent = send_desktop_notification(
            self.tr("测试通知"),
            self.tr("点击后将回到下载中心。"),
            target="download_center",
        )
        if sent:
            InfoBar.success(
                self.tr("测试通知已发送"),
                self.tr("如果没有看到通知，请检查系统通知权限。"),
                duration=4000,
                parent=self,
            )
        else:
            InfoBar.warning(
                self.tr("通知未发送"),
                self.tr("请检查系统设置中的通知权限。"),
                duration=5000,
                parent=self,
            )
        self.__refreshDesktopNotificationStatus()

    @staticmethod
    def __formatCookieStatusContent(result: dict) -> str:
        if result.get("status") == "missing":
            return "cookies.txt 不存在"

        content = result.get("message", "")
        source_label = result.get("source_browser_label")
        if source_label and source_label != "未知":
            content = f"{content} | 来源：{source_label}"
        cookie_count = result.get("cookie_count", 0)
        if cookie_count:
            content = f"{content} | {cookie_count} 条"
        if result.get("updated_at"):
            content = f"{content} | {result['updated_at']}"
        if result.get("has_youtube"):
            content = f"{content} | YouTube 域已覆盖"
        if result.get("has_bilibili_login"):
            content = f"{content} | B站登录已覆盖"
        elif result.get("has_bilibili"):
            content = f"{content} | B站仅访客 Cookie"
        return content

    def __showDownloadCenterOutputDir(self):
        self.__refreshDownloadCenterOutputDir()
        InfoBar.success(
            self.tr("下载中心输出目录"),
            self.downloadCenterOutputDirCard.contentLabel.text(),
            duration=2500,
            parent=self,
        )

    def refresh_cookie_status(self, result: dict | None = None):
        if result is None and self._cookie_export_in_progress:
            return
        if result is None:
            from app.core.utils.edge_cookie_utils import verify_cookie_file

            result = verify_cookie_file()
        self.edgeCookieStatusCard.setContent(self.__formatCookieStatusContent(result))

    def set_cookie_export_in_progress(self, in_progress: bool):
        self._cookie_export_in_progress = in_progress
        self.edgeCookieExportCard.button.setEnabled(not in_progress)
        self.edgeCookieStatusCard.button.setEnabled(not in_progress)
        self.edgeCookieExportCard.button.setText(
            self.tr("提取中…") if in_progress else self.tr("提取")
        )
        self.downloadCookieBrowserCard.comboBox.setEnabled(not in_progress)

    def __exportEdgeCookies(self):
        from app.core.utils.edge_cookie_utils import export_browser_cookies

        result = export_browser_cookies(
            browser=str(cfg.get(cfg.download_cookie_browser))
        )
        self.refresh_cookie_status(result)

        if result.get("success"):
            InfoBar.success(
                self.tr("Cookie 导出成功"),
                result.get("message", self.tr("已刷新 cookies.txt")),
                duration=2500,
                parent=self,
            )
        else:
            message = result.get("message", self.tr("无法从浏览器导出 Cookie"))
            if result.get("needs_elevation_hint"):
                from app.core.utils.edge_cookie_utils import (
                    MACOS_FULL_DISK_ACCESS_SETTINGS_URL,
                )

                dialog = MessageBox(
                    self.tr("需要完全磁盘访问权限"),
                    message,
                    self,
                )
                dialog.yesButton.setText(self.tr("打开系统设置"))
                dialog.cancelButton.setText(self.tr("稍后"))
                if dialog.exec():
                    QDesktopServices.openUrl(
                        QUrl(MACOS_FULL_DISK_ACCESS_SETTINGS_URL)
                    )
            else:
                InfoBar.error(
                    self.tr("Cookie 导出失败"),
                    message,
                    duration=5000,
                    parent=self,
                )

    def __showRestartTooltip(self):
        """显示重启提示"""
        InfoBar.success(
            self.tr("更新成功"),
            self.tr("配置将在重启后生效"),
            duration=1500,
            parent=self,
        )

    def __onsavePathCardClicked(self):
        """处理保存路径卡片点击事件"""
        folder = QFileDialog.getExistingDirectory(self, self.tr("选择文件夹"), "./")
        if not folder or cfg.get(cfg.work_dir) == folder:
            return
        cfg.set(cfg.work_dir, folder)
        self.savePathCard.setContent(folder)
        if not str(cfg.get(cfg.download_center_output_dir) or "").strip():
            self.__refreshDownloadCenterOutputDir()

    def __onDownloadProxyModeChanged(self, mode: str):
        self.downloadProxyUrlCard.setEnabled(mode == PROXY_MODE_MANUAL)
        self.__refreshDownloadProxyStatus()

    def __refreshDownloadProxyStatus(self):
        mode = self.downloadProxyModeCard.comboBox.currentText()
        proxy_url = get_effective_download_proxy_url(
            proxy_mode=mode,
            proxy_url=self.downloadProxyUrlCard.lineEdit.text(),
        )
        if mode == PROXY_MODE_OFF:
            content = self.tr("当前不使用代理")
        elif proxy_url:
            content = proxy_url
        else:
            content = self.tr("未检测到可用代理")
        self.downloadProxyStatusCard.setContent(content)

    def checkLLMConnection(self):
        """检查 LLM 连接"""
        # 获取当前选中的服务
        current_service = LLMServiceEnum(self.llmServiceCard.comboBox.currentText())

        # 获取服务配置
        service_config = self.llm_service_configs.get(current_service)
        if not service_config:
            return

        # 如果是公益模型，使用配置文件中的值
        if current_service == LLMServiceEnum.PUBLIC:
            api_base = cfg.public_api_base.value
            api_key = cfg.public_api_key.value
            model = cfg.public_model.value
        else:
            api_base = (
                service_config["api_base"].lineEdit.text()
                if service_config["api_base"]
                else ""
            )
            api_key = (
                service_config["api_key"].lineEdit.text()
                if service_config["api_key"]
                else ""
            )
            model = (
                service_config["model"].comboBox.currentText()
                if service_config["model"]
                else ""
            )
        qwen_enable_thinking = (
            service_config.get("qwen_thinking").isChecked()
            if service_config and service_config.get("qwen_thinking")
            else False
        )

        # 检查 API Base 是否属于网址
        if not api_base.startswith("http"):
            InfoBar.error(
                self.tr("错误"),
                self.tr("请输入正确的 API Base, 含有 /v1"),
                duration=3000,
                parent=self,
            )
            return

        # 禁用检查按钮，显示加载状态
        self.checkLLMConnectionCard.button.setEnabled(False)
        self.checkLLMConnectionCard.button.setText(self.tr("正在检查..."))

        # 创建并启动线程
        self.connection_thread = LLMConnectionThread(
            api_base,
            api_key,
            model,
            current_service.value,
            qwen_enable_thinking,
            cfg.llm_request_timeout.value,
        )
        self.connection_thread.finished.connect(self.onConnectionCheckFinished)
        self.connection_thread.error.connect(self.onConnectionCheckError)
        self.connection_thread.start()

    def onConnectionCheckError(self, message):
        """处理连接检查错误事件"""
        self.checkLLMConnectionCard.button.setEnabled(True)
        self.checkLLMConnectionCard.button.setText(self.tr("检查连接"))
        InfoBar.error(self.tr("LLM 连接测试错误"), message, duration=3000, parent=self)

    def onConnectionCheckFinished(self, is_success, message, models):
        """处理连接检查完成事件"""
        self.checkLLMConnectionCard.button.setEnabled(True)
        self.checkLLMConnectionCard.button.setText(self.tr("检查连接"))

        # 获取当前服务
        current_service = LLMServiceEnum(self.llmServiceCard.comboBox.currentText())

        if models:
            # 更新当前服务的模型列表
            service_config = self.llm_service_configs.get(current_service)
            if service_config and service_config["model"]:
                temp = service_config["model"].comboBox.currentText()
                service_config["model"].setItems(models)
                service_config["model"].comboBox.setCurrentText(temp)

            InfoBar.success(
                self.tr("获取模型列表成功:"),
                self.tr("一共") + str(len(models)) + self.tr("个模型"),
                duration=3000,
                parent=self,
            )
        if not is_success:
            InfoBar.error(
                self.tr("LLM 连接测试错误"), message, duration=3000, parent=self
            )
        else:
            InfoBar.success(
                self.tr("LLM 连接测试成功"), message, duration=3000, parent=self
            )

    def __onLLMServiceChanged(self, service):
        """处理LLM服务切换事件"""
        current_service = LLMServiceEnum(service)

        # 隐藏所有卡片
        for config in self.llm_service_configs.values():
            for card in config["cards"]:
                card.setVisible(False)

        # 隐藏OPENAI官方API链接卡片
        self.openaiOfficialApiCard.setVisible(False)

        # 显示选中服务的卡片
        if current_service in self.llm_service_configs:
            for card in self.llm_service_configs[current_service]["cards"]:
                card.setVisible(True)

            # 为OLLAMA和LM_STUDIO设置默认API Key
            service_config = self.llm_service_configs[current_service]
            if current_service == LLMServiceEnum.OLLAMA and service_config["api_key"]:
                # 如果API Key为空，设置默认值"ollama"
                if not service_config["api_key"].lineEdit.text():
                    service_config["api_key"].lineEdit.setText("ollama")
            if (
                current_service == LLMServiceEnum.LM_STUDIO
                and service_config["api_key"]
            ):
                # 如果API Key为空，设置默认值 "lm-studio"
                if not service_config["api_key"].lineEdit.text():
                    service_config["api_key"].lineEdit.setText("lm-studio")

            # 如果是OPENAI服务，显示官方API链接卡片
            if current_service == LLMServiceEnum.OPENAI:
                self.openaiOfficialApiCard.setVisible(True)

        # 更新布局
        self.llmGroup.adjustSize()
        self.expandLayout.update()

class LLMConnectionThread(QThread):
    finished = pyqtSignal(bool, str, list)
    error = pyqtSignal(str)

    def __init__(
        self,
        api_base,
        api_key,
        model,
        service_name=None,
        qwen_enable_thinking=False,
        timeout=300,
    ):
        super().__init__()
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.service_name = service_name
        self.qwen_enable_thinking = qwen_enable_thinking
        self.timeout = timeout

    def run(self):
        """检查 LLM 连接并获取模型列表"""
        try:
            from app.core.utils.test_opanai import get_openai_models, test_openai

            is_success, message = test_openai(
                self.api_base,
                self.api_key,
                self.model,
                self.service_name,
                self.qwen_enable_thinking,
                self.timeout,
            )
            models = get_openai_models(self.api_base, self.api_key, self.timeout)
            self.finished.emit(is_success, message, models)
        except Exception as e:
            self.error.emit(str(e))
