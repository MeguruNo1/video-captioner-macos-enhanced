import sys
import traceback
from pathlib import Path

from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import FluentTranslator

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.common.config import cfg
from app.config import RESOURCE_PATH
from app.core.utils import logger
from app.view.main_window import MainWindow

app_logger = logger.setup_logger("VideoCaptioner")


def exception_hook(exctype, value, tb):
    app_logger.error("".join(traceback.format_exception(exctype, value, tb)))
    sys.__excepthook__(exctype, value, tb)


def main():
    sys.excepthook = exception_hook

    if cfg.get(cfg.dpiScale) == "Auto":
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    else:
        import os

        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = str(cfg.get(cfg.dpiScale))
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)

    locale = cfg.get(cfg.language).value
    translator = FluentTranslator(locale)
    app_translator = QTranslator()
    translations_path = (
        RESOURCE_PATH / "translations" / f"VideoCaptioner_{locale.name()}.qm"
    )
    app_translator.load(str(translations_path))
    app.installTranslator(translator)
    app.installTranslator(app_translator)

    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
