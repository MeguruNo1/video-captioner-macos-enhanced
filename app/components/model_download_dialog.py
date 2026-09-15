from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, ComboBox, InfoBar, MessageBoxBase, PushSettingCard,
    ScrollArea, SubtitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from app.config import MODEL_PATH
from app.core.entities import TranscribeModelEnum


# Use repositories in the formats consumed by mlx-whisper and faster-whisper.
MODEL_DOWNLOADS = {
    "MLX Whisper": (
        ("Large v3 Turbo", "mlx-community/whisper-large-v3-turbo"),
        ("Large v3", "mlx-community/whisper-large-v3-mlx"),
        ("Distil Large v3（英语）", "mlx-community/distil-whisper-large-v3"),
        ("Medium", "mlx-community/whisper-medium"),
        ("Small", "mlx-community/whisper-small-mlx"),
        ("Base", "mlx-community/whisper-base-mlx"),
        ("Tiny", "mlx-community/whisper-tiny"),
    ),
    "WhisperX": (
        ("Large v3", "Systran/faster-whisper-large-v3"),
        ("Large v3 Turbo", "dropbox-dash/faster-whisper-large-v3-turbo"),
        ("Medium", "Systran/faster-whisper-medium"),
        ("Small", "Systran/faster-whisper-small"),
        ("Base", "Systran/faster-whisper-base"),
    ),
}


class ModelDownloadDialog(MessageBoxBase):
    def __init__(self, parent=None, engine=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("常用模型下载"))
        width = min(840, max(320, parent.width() - 48)) if parent else 840
        self.widget.setFixedWidth(width)
        self.viewLayout.addWidget(SubtitleLabel(self.tr("常用模型下载"), self))
        self.engineCombo = ComboBox(self)
        self.engineCombo.addItems(list(MODEL_DOWNLOADS))
        self.viewLayout.addWidget(self.engineCombo)
        self.descriptionLabel = BodyLabel(self)
        self.descriptionLabel.setWordWrap(True)
        self.viewLayout.addWidget(self.descriptionLabel)

        self.scrollArea = ScrollArea(self)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scrollArea.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scrollArea.setFixedHeight(min(400, max(120, parent.height() - 340)) if parent else 400)
        self.viewLayout.addWidget(self.scrollArea)
        self.yesButton.setText(self.tr("关闭"))
        self.cancelButton.hide()
        self.engineCombo.currentTextChanged.connect(self.populate_models)
        initial = "WhisperX" if engine == TranscribeModelEnum.WHISPER_X else "MLX Whisper"
        self.engineCombo.setCurrentText(initial)
        self.populate_models(initial)

    def populate_models(self, engine):
        if engine == "MLX Whisper":
            description = self.tr(
                "适用于 Apple 芯片 Mac。打开下载页后，将 config.json 和权重文件下载到同一文件夹，"
                "再到转录页的 MLX Whisper 设置中选择本地模型目录。"
            )
        else:
            description = self.tr(
                "适用于 CPU / NVIDIA CUDA。下载完整模型文件，存入下方目录的 "
                "faster-whisper-型号 子文件夹（如 faster-whisper-large-v3），再在转录页选择对应型号。\n"
            ) + str(MODEL_PATH)
        self.descriptionLabel.setText(description)
        previous = self.scrollArea.takeWidget()
        if previous is not None:
            previous.deleteLater()
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)
        self.modelCards = []
        for title, repo in MODEL_DOWNLOADS[engine]:
            card = PushSettingCard(self.tr("前往下载"), FIF.DOWNLOAD, title, repo, content)
            card.clicked.connect(lambda checked=False, repo=repo: self.open_download(repo))
            layout.addWidget(card)
            self.modelCards.append(card)
        layout.addStretch(1)
        self.scrollArea.setWidget(content)

    def open_download(self, repo):
        url = QUrl(f"https://huggingface.co/{repo}/tree/main")
        if not QDesktopServices.openUrl(url):
            InfoBar.error(
                self.tr("无法打开浏览器"), self.tr("请复制下载地址到浏览器：") + url.toString(),
                duration=6000, parent=self,
            )
