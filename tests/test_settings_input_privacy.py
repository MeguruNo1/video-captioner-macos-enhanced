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
