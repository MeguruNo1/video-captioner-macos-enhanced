"""Qt adapter for the shared download component updater."""
from PyQt5.QtCore import QThread, pyqtSignal

from app.core.download_components import check_updates, install_updates, rollback


class DownloadComponentUpdateThread(QThread):
    completed = pyqtSignal(dict)
    progress = pyqtSignal(str)

    def __init__(self, action: str, proxy: str, parent=None):
        super().__init__(parent)
        self.action = action
        self.proxy = proxy

    def run(self):
        try:
            if self.action == "rollback":
                result = rollback()
            elif self.action == "install":
                result = install_updates(self.proxy, progress=self.progress.emit)
            else:
                result = check_updates(self.proxy)
                if self.action == "auto" and result["available"]:
                    result = install_updates(self.proxy, progress=self.progress.emit)
            self.completed.emit({"success": True, **result})
        except Exception as exc:
            self.completed.emit({"success": False, "message": str(exc)})
