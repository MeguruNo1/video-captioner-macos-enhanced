import os
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication, QLineEdit
from qfluentwidgets import FluentIcon, PasswordLineEdit
from qfluentwidgets.common.config import ConfigItem
from app.common.config import cfg
from app.components.LineEditSettingCard import LineEditSettingCard
from app.view.setting_interface import SettingInterface

_APP = QApplication.instance() or QApplication([])


def test_password_card_masks_without_changing_saved_value(tmp_path):
    item = ConfigItem('Audit', 'Key', 'test-only-key')
    with patch.object(cfg._cfg, 'file', tmp_path / 'settings.json'):
        card = LineEditSettingCard(item, FluentIcon.FINGERPRINT, 'API Key', password=True)
        assert isinstance(card.lineEdit, PasswordLineEdit)
        assert card.lineEdit.echoMode() == QLineEdit.Password
        assert card.lineEdit.text() == item.value == 'test-only-key'
        card.lineEdit.setPasswordVisible(True)
        assert card.lineEdit.echoMode() == QLineEdit.Normal
        card.lineEdit.setPasswordVisible(False)
        card.lineEdit.setText('updated-test-key')
        assert item.value == 'updated-test-key'
        assert card.lineEdit.displayText() != item.value
        card.deleteLater()


def test_every_provider_key_is_masked_but_urls_remain_readable(tmp_path):
    with patch.object(cfg._cfg, 'file', tmp_path / 'settings.json'):
        page = SettingInterface()
        for provider in page.llm_service_configs.values():
            if provider['api_key']:
                assert provider['api_key'].lineEdit.echoMode() == QLineEdit.Password
                assert provider['api_base'].lineEdit.echoMode() == QLineEdit.Normal
        page.deleteLater()


def test_settings_scrolls_with_pixel_only_trackpad_events(tmp_path):
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QWheelEvent
    with patch.object(cfg._cfg, 'file', tmp_path / 'settings.json'):
        page = SettingInterface()
        page.resize(800, 600)
        with patch.object(page, "_SettingInterface__refreshDesktopNotificationStatus"):
            page.show()
            _APP.processEvents()
        bar = page.verticalScrollBar()
        assert bar.maximum() > 0
        before = bar.value()
        event = QWheelEvent(QPointF(20, 20), QPointF(page.viewport().mapToGlobal(QPoint(20, 20))),
                            QPoint(0, -120), QPoint(), Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        QApplication.sendEvent(page.viewport(), event)
        _APP.processEvents()
        assert bar.value() > before
        page.deleteLater()
