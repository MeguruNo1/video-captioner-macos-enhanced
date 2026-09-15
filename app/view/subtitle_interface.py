# -*- coding: utf-8 -*-
import os
import re
import sys
import tempfile
import json
from pathlib import Path

from PyQt5.QtCore import Qt, QTime, QUrl, QAbstractTableModel, QEvent, pyqtSignal
from PyQt5.QtGui import QColor, QDragEnterEvent, QDropEvent, QPalette
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QSplitter,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import Action, BodyLabel, CommandBar
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    TableView,
    TextEdit,
    TransparentDropDownPushButton,
    isDarkTheme,
    themeColor,
)

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.components.SubtitleSettingDialog import SubtitleSettingDialog
from app.core.bk_asr.asr_data import ASRData
from app.core.entities import (
    OutputSubtitleFormatEnum,
    SubtitleTask,
    SupportedSubtitleFormats,
    TargetLanguageEnum,
)
from app.core.task_factory import TaskFactory
from app.core.utils.desktop_notification import send_desktop_notification
from app.view.setting_interface import PromptCenterDialog


class SubtitleTableModel(QAbstractTableModel):
    def __init__(self, data="", view_mode="original"):
        super().__init__()
        self._data = {}
        self.view_mode = view_mode
        if isinstance(data, str):
            self.load_data(data)
        else:
            self._data = data

    @staticmethod
    def _format_time(ms: int) -> str:
        return QTime(0, 0).addMSecs(ms).toString("hh:mm:ss.zzz")[:-2]

    def _field_name(self) -> str:
        if self.view_mode == "translated":
            return "translated_subtitle"
        return "original_subtitle"

    def load_data(self, data: str):
        """加载字幕数据"""
        try:
            self._data = json.loads(data)
            self.layoutChanged.emit()
        except json.JSONDecodeError:
            pass

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not self._data:
            return None

        row = index.row()
        segment = self._data.get(str(row + 1))

        if not segment:
            return None

        field_name = self._field_name()
        if role == Qt.EditRole:
            return segment.get(field_name, "")
        if role == Qt.DisplayRole:
            start = self._format_time(segment["start_time"])
            end = self._format_time(segment["end_time"])
            text = segment.get(field_name, "")
            return f"{start} - {end}\n{text}"
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignLeft | Qt.AlignVCenter
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or not self._data:
            return False

        if role == Qt.EditRole:
            row = index.row()
            segment = self._data.get(str(row + 1))

            if not segment:
                return False

            segment[self._field_name()] = value

            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
            return True
        return False

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                if self.view_mode == "translated":
                    return (
                        self.tr("译文")
                        if cfg.need_translate.value
                        else self.tr("优化结果")
                    )
                return self.tr("原文")
            elif orientation == Qt.Vertical:
                return str(section + 1)  # 显示行号
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignCenter  # 居中对齐
        return None

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return 1

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def update_data(self, new_data):
        """更新字幕数据"""
        updated_rows = set()

        # 更新内部数据
        for key, value in new_data.items():
            if key in self._data:
                if "||" in value:
                    original_subtitle, translated_subtitle = value.split("||", 1)
                    self._data[key]["original_subtitle"] = original_subtitle
                    self._data[key]["translated_subtitle"] = translated_subtitle
                else:
                    self._data[key]["translated_subtitle"] = value
                row = list(self._data.keys()).index(key)
                updated_rows.add(row)

        # 如果有更新，发出dataChanged信号
        if updated_rows:
            min_row = min(updated_rows)
            max_row = max(updated_rows)
            top_left = self.index(min_row, 0)
            bottom_right = self.index(max_row, 0)
            self.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole, Qt.EditRole])

    def update_all(self, data: dict):
        """更新所有数据"""
        self._data = data
        self.layoutChanged.emit()


class SubtitleInterface(QWidget):
    finished = pyqtSignal(str, str)
    COMPACT_LAYOUT_WIDTH = 760

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SubtitleInterface")
        self.setAcceptDrops(True)
        self.task = None
        self.subtitle_path = None
        self.custom_prompt_text = cfg.custom_prompt_text.value
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._init_ui()
        self._setup_signals()
        self._update_prompt_button_style()
        self.set_values()
        cfg.themeMode.valueChanged.connect(lambda *_: self._apply_theme_styles())
        cfg.themeColor.valueChanged.connect(lambda *_: self._refresh_accent_icons())
        cfg.themeColor.valueChanged.connect(lambda *_: self._apply_theme_styles())
        self._apply_theme_styles()
        self._adjust_responsive_layout()

    @staticmethod
    def _extract_supported_subtitle_paths(urls):
        supported_formats = {fmt.value for fmt in SupportedSubtitleFormats}
        valid_files = []
        for url in urls:
            file_path = url.toLocalFile()
            if not file_path or not os.path.isfile(file_path):
                continue
            file_ext = os.path.splitext(file_path)[1][1:].lower()
            if file_ext in supported_formats:
                valid_files.append(file_path)
        return valid_files

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setObjectName("main_layout")
        self.main_layout.setSpacing(20)

        self._setup_top_layout()
        self._setup_subtitle_table()
        self._setup_bottom_layout()

    def set_values(self):
        self.layout_button.setText(cfg.subtitle_layout.value)
        self.optimize_button.setChecked(cfg.need_optimize.value)
        self._update_translation_button_state()

    def _setup_top_layout(self):
        # 创建水平布局
        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)

        # 创建命令栏
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon
        )  # 设置图标和文字并排显示
        self.command_bar.setFixedHeight(40)
        top_layout.addWidget(self.command_bar, 1)  # 设置stretch为1，使其尽可能占用空间

        self.command_bar.addAction(
            Action(FIF.FOLDER, self.tr("打开文件"), triggered=self.on_file_select)
        )
        self.command_bar.addSeparator()

        # 创建保存按钮的下拉菜单
        save_menu = RoundMenu(parent=self)
        save_menu.view.setMaxVisibleItems(8)  # 设置菜单最大高度
        for format in OutputSubtitleFormatEnum:
            action = Action(text=format.value)
            action.triggered.connect(
                lambda checked, f=format.value: self.on_save_format_clicked(f)
            )
            save_menu.addAction(action)

        # 添加保存按钮(带下拉菜单)
        self.save_button = TransparentDropDownPushButton(
            self.tr("保存"), self, FIF.SAVE
        )
        self.save_button.setMenu(save_menu)
        self.save_button.setFixedHeight(34)
        self.save_button.setMinimumWidth(90)
        self.save_button.setToolTip(self.tr("请先加载字幕文件"))
        self.save_button.setEnabled(False)
        self.command_bar.addWidget(self.save_button)

        # 添加字幕排布下拉按钮
        self.layout_button = TransparentDropDownPushButton(
            self.tr("字幕排布"), self, FIF.LAYOUT
        )
        self.layout_button.setFixedHeight(34)
        self.layout_button.setMinimumWidth(125)
        self.layout_menu = RoundMenu(parent=self)
        for layout in ["译文在上", "原文在上", "仅译文", "仅原文", "单独输出原文和译文"]:
            action = Action(text=layout)
            action.triggered.connect(
                lambda checked, l=layout: signalBus.subtitle_layout_changed.emit(l)
            )
            self.layout_menu.addAction(action)
        self.layout_button.setMenu(self.layout_menu)
        self.command_bar.addWidget(self.layout_button)

        self.command_bar.addSeparator()

        # 添加字幕优化按钮
        self.optimize_button = Action(
            FIF.EDIT,
            self.tr("字幕校正"),
            triggered=self.on_subtitle_optimization_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.optimize_button)

        # 添加字幕翻译与目标语种菜单
        self.translation_button = TransparentDropDownPushButton(
            self.tr("字幕翻译"), self, FIF.LANGUAGE
        )
        self.translation_button.setFixedHeight(34)
        self.translation_button.setMinimumWidth(160)
        self.translation_menu = RoundMenu(parent=self)
        self.translation_menu.setMaxVisibleItems(12)
        self.translation_disabled_action = Action(
            FIF.LANGUAGE,
            self.tr("关闭字幕翻译"),
            triggered=lambda checked: self.on_subtitle_translation_changed(False),
            checkable=True,
        )
        self.translation_menu.addAction(self.translation_disabled_action)
        self.translation_menu.addSeparator()
        self.target_language_actions = []
        for lang in TargetLanguageEnum:
            action = Action(FIF.LANGUAGE, lang.value, checkable=True)
            action.triggered.connect(
                lambda checked, l=lang.value: self.select_translation_language(l)
            )
            self.target_language_actions.append(action)
            self.translation_menu.addAction(action)
        self.translation_button.setMenu(self.translation_menu)
        self.command_bar.addWidget(self.translation_button)

        self.command_bar.addSeparator()

        # 添加文稿提示菜单
        self.prompt_button = TransparentDropDownPushButton(
            self.tr("文稿提示"), self, FIF.DOCUMENT
        )
        self.prompt_button.setFixedHeight(34)
        self.prompt_button.setMinimumWidth(125)
        self.prompt_menu = RoundMenu(parent=self)
        self.prompt_menu.addAction(
            Action(FIF.DOCUMENT, self.tr("文稿提示"), triggered=self.show_prompt_dialog)
        )
        self.prompt_menu.addAction(
            Action(
                FIF.SETTING,
                self.tr("提示词中心"),
                triggered=self.show_prompt_center_dialog,
            )
        )
        self.prompt_button.setMenu(self.prompt_menu)
        self.command_bar.addWidget(self.prompt_button)
        self.full_script_button = Action(
            FIF.DOCUMENT, self.tr("查看全文"), triggered=self.show_full_script_dialog
        )
        self.full_script_button.setEnabled(False)
        self.command_bar.addAction(self.full_script_button)

        # 添加设置按钮
        self.command_bar.addAction(
            Action(FIF.SETTING, "", triggered=self.show_subtitle_settings)
        )

        # 添加视频播放按钮
        # self.command_bar.addAction(Action(FIF.VIDEO, "", triggered=self.show_video_player))

        # 添加开始按钮到水平布局
        self.start_button = PrimaryPushButton(self.tr("开始"), self, icon=FIF.PLAY)
        self.start_button.clicked.connect(
            lambda: self.start_subtitle_optimization(need_create_task=True)
        )
        self.start_button.setFixedHeight(34)
        self.start_button.setEnabled(False)
        self.start_button.setToolTip(self.tr("请先加载字幕文件"))
        top_layout.addWidget(self.start_button)

        self.main_layout.addLayout(top_layout)

    def _setup_subtitle_table(self):
        self.content_splitter = QSplitter(Qt.Horizontal, self)
        self.content_splitter.setHandleWidth(1)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setStyleSheet(
            """
            QSplitter::handle {
                background: transparent;
                border: none;
            }
            QSplitter::handle:horizontal {
                width: 1px;
            }
            QSplitter::handle:vertical {
                height: 4px;
            }
            """
        )
        self.original_table = TableView(self)
        self.subtitle_table = TableView(self)
        self.log_text = TextEdit(self)
        self.original_table.setObjectName("subtitleOriginalTable")
        self.subtitle_table.setObjectName("subtitleTranslatedTable")
        self.log_text.setObjectName("subtitleLogText")
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText(
            self.tr("处理日志将在此显示\n\n先打开或拖入字幕文件，再选择字幕校正或翻译。")
        )

        self.original_model = SubtitleTableModel("", "original")
        self.translation_model = SubtitleTableModel("", "translated")
        self.model = self.translation_model
        self.original_table.setModel(self.original_model)
        self.subtitle_table.setModel(self.translation_model)

        for table in (self.original_table, self.subtitle_table):
            table.setAcceptDrops(True)
            table.viewport().setAcceptDrops(True)
            table.installEventFilter(self)
            table.viewport().installEventFilter(self)
            table.setBorderVisible(True)
            table.setBorderRadius(8)
            table.setWordWrap(True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            table.verticalHeader().setVisible(True)
            table.verticalHeader().setDefaultAlignment(Qt.AlignCenter)
            table.verticalHeader().setDefaultSectionSize(72)
            table.verticalHeader().setMinimumWidth(24)
            table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            )
            table.clicked.connect(self.on_subtitle_clicked)
            table.setContextMenuPolicy(Qt.CustomContextMenu)
            table.customContextMenuRequested.connect(self.show_context_menu)

        self._setup_synced_subtitle_scrollbars()

        self.content_splitter.addWidget(self.original_table)
        self.content_splitter.addWidget(self.subtitle_table)
        self.content_splitter.addWidget(self.log_text)
        self.content_splitter.setStretchFactor(0, 3)
        self.content_splitter.setStretchFactor(1, 3)
        self.content_splitter.setStretchFactor(2, 2)
        self.main_layout.addWidget(self.content_splitter, 1)

    def _apply_theme_styles(self):
        if isDarkTheme():
            page_background = "#202124"
            panel_background = "rgba(255, 255, 255, 0.04)"
            table_background = "rgba(255, 255, 255, 0.04)"
            border_color = "rgba(255, 255, 255, 0.08)"
            grid_color = "rgba(255, 255, 255, 0.07)"
            header_background = "rgba(255, 255, 255, 0.06)"
            text_color = "#F5F5F5"
            table_base_background = "#292A2D"
            alternate_row_background = "#303134"
            handle_color = "rgba(255, 255, 255, 0.12)"
        else:
            page_background = "#F5F7FA"
            panel_background = "#FFFFFF"
            table_background = "#FFFFFF"
            border_color = "rgba(17, 24, 39, 0.12)"
            grid_color = "rgba(17, 24, 39, 0.10)"
            header_background = "#F2F4F7"
            text_color = "#1D2939"
            table_base_background = "#FFFFFF"
            alternate_row_background = "#F7F8FA"
            handle_color = "rgba(17, 24, 39, 0.16)"

        accent_color = themeColor()
        selection_background = accent_color.name()
        selection_text = "#102124" if accent_color.lightness() >= 160 else "#FFFFFF"

        self.setStyleSheet(
            f"""
            QWidget#SubtitleInterface {{
                background-color: {page_background};
            }}
            QTableView#subtitleOriginalTable,
            QTableView#subtitleTranslatedTable,
            QTextEdit#subtitleLogText {{
                background-color: {table_background};
                color: {text_color};
                border: 1px solid {border_color};
                border-radius: 8px;
                gridline-color: {grid_color};
            }}
            QHeaderView::section {{
                background-color: {header_background};
                color: {text_color};
                border: none;
                border-bottom: 1px solid {border_color};
                padding: 6px;
            }}
            QSplitter::handle {{
                background: {handle_color};
                border: none;
            }}
            """
        )
        self.content_splitter.setStyleSheet(
            f"""
            QSplitter::handle {{
                background: {handle_color};
                border: none;
            }}
            QSplitter::handle:horizontal {{
                width: 1px;
            }}
            QSplitter::handle:vertical {{
                height: 4px;
            }}
            """
        )
        for table in (self.original_table, self.subtitle_table):
            palette = table.palette()
            palette.setColor(QPalette.Base, QColor(table_base_background))
            palette.setColor(
                QPalette.AlternateBase, QColor(alternate_row_background)
            )
            palette.setColor(QPalette.Text, QColor(text_color))
            palette.setColor(QPalette.Highlight, accent_color)
            palette.setColor(QPalette.HighlightedText, QColor(selection_text))
            table.setPalette(palette)
            table.setAlternatingRowColors(True)
            table.setStyleSheet(
                f"""
                QTableView {{
                    background-color: {table_base_background};
                    alternate-background-color: {alternate_row_background};
                    color: {text_color};
                    border: 1px solid {border_color};
                    border-radius: 8px;
                    gridline-color: {grid_color};
                }}
                QTableView::item:selected {{
                    background-color: {selection_background};
                    color: {selection_text};
                }}
                QHeaderView::section {{
                    background-color: {header_background};
                    color: {text_color};
                    border: none;
                    border-bottom: 1px solid {border_color};
                    padding: 6px;
                }}
                """
            )
        self.log_text.setStyleSheet(
            f"""
            QTextEdit {{
                background-color: {panel_background};
                color: {text_color};
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
            """
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._adjust_responsive_layout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_responsive_layout()

    def _adjust_responsive_layout(self):
        if not hasattr(self, "content_splitter"):
            return

        target_orientation = (
            Qt.Vertical
            if self.width() <= self.COMPACT_LAYOUT_WIDTH
            else Qt.Horizontal
        )
        if self.content_splitter.orientation() == target_orientation:
            return

        self.content_splitter.setOrientation(target_orientation)
        self.content_splitter.setHandleWidth(
            4 if target_orientation == Qt.Vertical else 1
        )
        available_size = (
            max(self.content_splitter.height(), 1)
            if target_orientation == Qt.Vertical
            else max(self.content_splitter.width(), 1)
        )
        self.content_splitter.setSizes(
            [
                int(available_size * 0.36),
                int(available_size * 0.36),
                int(available_size * 0.28),
            ]
        )

    def _setup_synced_subtitle_scrollbars(self):
        self._syncing_subtitle_scrollbars = False
        self.original_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.subtitle_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        original_bar = self.original_table.verticalScrollBar()
        subtitle_bar = self.subtitle_table.verticalScrollBar()
        original_bar.valueChanged.connect(
            lambda _: self._sync_subtitle_scrollbar(
                self.original_table, self.subtitle_table
            )
        )
        subtitle_bar.valueChanged.connect(
            lambda _: self._sync_subtitle_scrollbar(
                self.subtitle_table, self.original_table
            )
        )
        original_bar.rangeChanged.connect(
            lambda *_: self._sync_subtitle_scrollbar(
                self.subtitle_table, self.original_table
            )
        )
        subtitle_bar.rangeChanged.connect(
            lambda *_: self._sync_subtitle_scrollbar(
                self.original_table, self.subtitle_table
            )
        )

    def _sync_subtitle_scrollbar(self, source_table, target_table):
        if self._syncing_subtitle_scrollbars:
            return

        source_bar = source_table.verticalScrollBar()
        target_bar = target_table.verticalScrollBar()
        source_max = source_bar.maximum()
        target_max = target_bar.maximum()
        if target_max <= 0:
            target_value = 0
        elif source_max <= 0:
            target_value = min(source_bar.value(), target_max)
        else:
            target_value = round(source_bar.value() / source_max * target_max)

        if target_bar.value() == target_value:
            return

        self._syncing_subtitle_scrollbars = True
        try:
            target_bar.setValue(target_value)
        finally:
            self._syncing_subtitle_scrollbars = False

    def _setup_bottom_layout(self):
        self.bottom_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("请拖入字幕文件"), self)
        self.status_label.setMinimumWidth(100)
        self.status_label.setAlignment(Qt.AlignCenter)

        # 添加取消按钮
        self.cancel_button = PushButton(self.tr("取消"), self, icon=FIF.CANCEL)
        self.cancel_button.hide()  # 初始隐藏
        self.cancel_button.clicked.connect(self.cancel_optimization)

        self.bottom_layout.addWidget(self.progress_bar, 1)
        self.bottom_layout.addWidget(self.status_label)
        self.bottom_layout.addWidget(self.cancel_button)
        self.main_layout.addLayout(self.bottom_layout)

    def _setup_signals(self):
        signalBus.subtitle_layout_changed.connect(self.on_subtitle_layout_changed)
        signalBus.target_language_changed.connect(self.on_target_language_changed)
        signalBus.subtitle_optimization_changed.connect(
            self.on_subtitle_optimization_changed
        )
        signalBus.subtitle_translation_changed.connect(
            self.on_subtitle_translation_changed
        )
        # self.subtitle_setting_button.clicked.connect(self.show_subtitle_settings)
        # self.video_player_button.clicked.connect(self.show_video_player)

    def show_prompt_dialog(self):
        dialog = PromptDialog(self)
        if dialog.exec_():
            self.custom_prompt_text = cfg.custom_prompt_text.value
            self._update_prompt_button_style()

    def show_prompt_center_dialog(self):
        dialog = PromptCenterDialog(self)
        dialog.exec_()
        self.custom_prompt_text = cfg.custom_prompt_text.value
        self._update_prompt_button_style()

    def _update_prompt_button_style(self):
        if self.custom_prompt_text.strip():
            accent_color = self._theme_accent_color()
            self.prompt_button.setIcon(FIF.DOCUMENT.colored(accent_color, accent_color))
        else:
            self.prompt_button.setIcon(FIF.DOCUMENT)

    @staticmethod
    def _theme_accent_color() -> QColor:
        color = cfg.themeColor.value
        if isinstance(color, QColor):
            return QColor(color)
        return QColor(str(color))

    def _refresh_accent_icons(self):
        self._update_prompt_button_style()
        self._update_translation_button_state()

    def _update_full_script_button_state(self):
        self.full_script_button.setEnabled(bool(self.model._data))

    @staticmethod
    def _is_cjk_char(char: str) -> bool:
        if not char:
            return False
        code = ord(char)
        return (
            0x4E00 <= code <= 0x9FFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        )

    @classmethod
    def _join_script_fragments(cls, fragments):
        text = ""
        no_space_before = set(".,!?;:)]}，。！？；：、】【」』》％%")
        no_space_after = set("([{\"“‘【「『《")
        for fragment in fragments:
            fragment = (fragment or "").strip()
            if not fragment:
                continue
            if not text:
                text = fragment
                continue
            if (
                cls._is_cjk_char(text[-1])
                or cls._is_cjk_char(fragment[0])
                or fragment[0] in no_space_before
                or text[-1] in no_space_after
            ):
                text += fragment
            else:
                text += f" {fragment}"
        return re.sub(r"\s+\n", "\n", text).strip()

    @classmethod
    def _build_full_script_text(
        cls, subtitle_data: dict, layout: str = None, display_mode: str = "auto"
    ) -> str:
        if not subtitle_data:
            return ""

        segments = [
            subtitle_data[key]
            for key in sorted(subtitle_data.keys(), key=lambda item: int(item))
        ]
        has_translation = any(
            (segment.get("translated_subtitle") or "").strip() for segment in segments
        )
        layout = layout or cfg.subtitle_layout.value

        if display_mode == "auto":
            if not has_translation or layout == "仅原文":
                display_mode = "original"
            elif layout in ["仅译文", "单独输出原文和译文"]:
                display_mode = "translated"
            else:
                display_mode = "bilingual"

        blocks = []
        current_speaker = None
        original_parts = []
        translated_parts = []

        for segment in segments:
            speaker = (segment.get("speaker") or "").strip()
            if current_speaker is None:
                current_speaker = speaker
            elif speaker != current_speaker:
                blocks.append((current_speaker, original_parts, translated_parts))
                original_parts = []
                translated_parts = []
                current_speaker = speaker

            original_parts.append(segment.get("original_subtitle", ""))
            translated_parts.append(segment.get("translated_subtitle", ""))

        if original_parts or translated_parts:
            blocks.append((current_speaker or "", original_parts, translated_parts))

        paragraphs = []
        for speaker, original_parts, translated_parts in blocks:
            original_text = cls._join_script_fragments(original_parts)
            translated_text = cls._join_script_fragments(translated_parts)
            if not original_text and not translated_text:
                continue

            speaker_label = f"[{speaker}] " if speaker else ""
            if not has_translation or display_mode == "original":
                paragraphs.append(f"{speaker_label}{original_text}".strip())
                continue
            if display_mode == "translated":
                paragraphs.append(
                    f"{speaker_label}{translated_text or original_text}".strip()
                )
                continue

            first_line = original_text
            second_line = translated_text or original_text
            if layout == "译文在上":
                first_line, second_line = second_line, first_line

            paragraph_lines = [f"{speaker_label}{first_line}".strip()]
            if second_line:
                paragraph_lines.append(second_line)
            paragraphs.append("\n".join(paragraph_lines).strip())

        return "\n\n".join(paragraphs).strip()

    def show_full_script_dialog(self):
        if not self.model._data:
            InfoBar.warning(
                self.tr("警告"), self.tr("请先加载字幕文件"), duration=3000, parent=self
            )
            return

        dialog = FullScriptDialog(self.model._data, cfg.subtitle_layout.value, self)
        dialog.exec_()

    def _set_subtitle_data(self, data: dict):
        self.original_model.update_all(data)
        self.translation_model.update_all(data)
        self.model = self.translation_model
        self._update_full_script_button_state()

    def append_task_log(self, message: str):
        message = str(message or "").strip()
        if not message:
            return
        timestamp = QTime.currentTime().toString("HH:mm:ss")
        self.log_text.append(f"[{timestamp}] {message}")

    def set_task(self, task: SubtitleTask):
        """设置任务并更新UI"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()
        self.start_button.setEnabled(True)
        self.start_button.setText(self.tr("开始"))
        self.start_button.setToolTip(self.tr("开始处理当前字幕"))
        self.save_button.setEnabled(True)
        self.save_button.setToolTip(self.tr("保存字幕"))
        self.task = task
        self.subtitle_path = task.subtitle_path
        self.update_info(task)

    def update_info(self, task: SubtitleTask):
        """更新页面信息"""
        original_subtitle_save_path = Path(self.task.subtitle_path)
        asr_data = ASRData.from_subtitle_file(original_subtitle_save_path)
        self._set_subtitle_data(asr_data.to_json())
        self.status_label.setText(self.tr("已加载文件"))
        self.append_task_log(self.tr("已加载字幕"))

    def start_subtitle_optimization(self, need_create_task=True):
        # 检查是否有任务
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"), self.tr("请先加载字幕文件"), duration=3000, parent=self
            )
            return
        self.start_button.setEnabled(False)
        self.start_button.setText(self.tr("处理中…"))
        self.progress_bar.reset()
        self.cancel_button.show()
        self.status_label.setToolTip("")
        self.log_text.clear()
        self.append_task_log(self.tr("开始字幕处理"))

        if need_create_task:
            self.task = TaskFactory.create_subtitle_task(file_path=self.subtitle_path)
        elif self.task and self.task.subtitle_config:
            self.task.subtitle_config.need_mask_original_profanity = (
                cfg.need_mask_original_profanity.value
            )
        from app.thread.subtitle_thread import SubtitleThread

        self.subtitle_optimization_thread = SubtitleThread(self.task)
        self.subtitle_optimization_thread.finished.connect(
            self.on_subtitle_optimization_finished
        )
        self.subtitle_optimization_thread.progress.connect(
            self.on_subtitle_optimization_progress
        )
        self.subtitle_optimization_thread.token_progress.connect(
            self.on_subtitle_token_progress
        )
        self.subtitle_optimization_thread.update.connect(self.update_data)
        self.subtitle_optimization_thread.update_all.connect(self.update_all)
        self.subtitle_optimization_thread.error.connect(
            self.on_subtitle_optimization_error
        )
        self.subtitle_optimization_thread.set_custom_prompt_text(
            self.custom_prompt_text
        )
        self.subtitle_optimization_thread.start()

    def process(self):
        """主处理函数"""
        # 检查是否有任务
        self.start_subtitle_optimization(need_create_task=False)

    def on_subtitle_optimization_finished(self, video_path, output_path):
        self.start_button.setEnabled(True)
        self.start_button.setText(self.tr("再次处理"))
        self.start_button.setToolTip(self.tr("再次处理当前字幕"))
        self.cancel_button.hide()  # 隐藏取消按钮
        self.progress_bar.setValue(100)
        self.status_label.setText(self.tr("处理完成"))
        self.status_label.setToolTip(str(output_path))
        if self.task.need_next_task:
            self.finished.emit(video_path, output_path)
        self.append_task_log(
            self.tr("任务完成: ") + output_path + self._format_token_usage_log_suffix()
        )
        InfoBar.success(
            self.tr("优化完成"),
            self.tr("优化完成字幕..."),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
            parent=self.parent(),
        )
        send_desktop_notification(
            self.tr("字幕处理完成"),
            self.tr("字幕文件已生成：") + Path(output_path).name,
            target="subtitle",
        )

    def on_subtitle_optimization_error(self, error):
        self.start_button.setEnabled(True)
        self.start_button.setText(self.tr("重试"))
        self.start_button.setToolTip(self.tr("重新处理当前字幕"))
        self.cancel_button.hide()  # 隐藏取消按钮
        self.progress_bar.error()
        self.status_label.setText(self.tr("处理失败"))
        self.status_label.setToolTip(str(error))
        self.append_task_log(self.tr("错误: ") + str(error))
        InfoBar.error(self.tr("优化失败"), self.tr(error), duration=20000, parent=self)
        send_desktop_notification(
            self.tr("字幕处理失败"),
            str(error),
            target="subtitle",
        )

    def on_subtitle_optimization_progress(self, value, status):
        self.progress_bar.setValue(value)
        self.status_label.setText(status)
        self.append_task_log(status)

    def on_subtitle_token_progress(self, status):
        self.status_label.setText(status)

    def _format_token_usage_log_suffix(self) -> str:
        thread = getattr(self, "subtitle_optimization_thread", None)
        usage = getattr(thread, "token_usage", None) or {}
        total = int(usage.get("total_tokens", 0) or 0)
        if total <= 0:
            return ""

        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        return self.tr(" | Token 总消耗: {0}（输入 {1} / 输出 {2}）").format(
            total,
            prompt_tokens,
            completion_tokens,
        )

    def update_data(self, data):
        self.original_model.update_data(data)
        self.translation_model.update_data(data)
        self._update_full_script_button_state()

    def update_all(self, data):
        self._set_subtitle_data(data)

    def remove_widget(self):
        """隐藏顶部开始按钮和底部进度条"""
        self.start_button.hide()
        for i in range(self.bottom_layout.count()):
            widget = self.bottom_layout.itemAt(i).widget()
            if widget:
                widget.hide()

    def on_file_select(self):
        # 构建文件过滤器
        subtitle_formats = " ".join(
            f"*.{fmt.value}" for fmt in SupportedSubtitleFormats
        )
        filter_str = f"{self.tr('字幕文件')} ({subtitle_formats})"

        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择字幕文件"), "", filter_str
        )
        if file_path:
            self.subtitle_path = file_path
            self.load_subtitle_file(file_path)

    def on_save_format_clicked(self, format: str):
        """处理保存格式的选择"""
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"), self.tr("请先加载字幕文件"), duration=3000, parent=self
            )
            return

        # 获取保存路径
        default_name = Path(self.subtitle_path).stem
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("保存字幕文件"),
            default_name,  # 使用原文件名作为默认名
            f"{self.tr('字幕文件')} (*.{format})",
        )
        if not file_path:
            return

        try:
            # 转换并保存字幕
            asr_data = ASRData.from_json(self.model._data)
            layout = cfg.subtitle_layout.value

            asr_data.save(file_path, layout=layout)
            self.append_task_log(self.tr("字幕已保存: ") + file_path)
            if layout == "单独输出原文和译文":
                target_path = Path(file_path)
                self.append_task_log(
                    self.tr("额外输出: ")
                    + str(
                        target_path.with_name(
                            f"{target_path.stem}-仅原文{target_path.suffix}"
                        )
                    )
                )
                self.append_task_log(
                    self.tr("额外输出: ")
                    + str(
                        target_path.with_name(
                            f"{target_path.stem}-仅译文{target_path.suffix}"
                        )
                    )
                )
            InfoBar.success(
                self.tr("保存成功"),
                self.tr("字幕已保存"),
                duration=3000,
                parent=self,
            )
        except Exception as e:
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("保存字幕文件失败: ") + str(e),
                duration=5000,
                parent=self,
            )

    def load_subtitle_file(self, file_path):
        self.subtitle_path = file_path
        asr_data = ASRData.from_subtitle_file(file_path)
        self._set_subtitle_data(asr_data.to_json())
        self.start_button.setEnabled(True)
        self.start_button.setText(self.tr("开始"))
        self.start_button.setToolTip(self.tr("开始处理当前字幕"))
        self.save_button.setEnabled(True)
        self.save_button.setToolTip(self.tr("保存字幕"))
        self.status_label.setText(self.tr("已加载文件"))
        self.append_task_log(self.tr("已加载字幕: ") + os.path.basename(file_path))

    def dragEnterEvent(self, event: QDragEnterEvent):
        files = self._extract_supported_subtitle_paths(event.mimeData().urls())
        if files:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        files = self._extract_supported_subtitle_paths(event.mimeData().urls())
        if files:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        files = self._extract_supported_subtitle_paths(event.mimeData().urls())
        if not files:
            InfoBar.error(
                self.tr("格式错误"),
                self.tr("仅支持拖入字幕文件（srt / ass / vtt）。"),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
            event.ignore()
            return

        file_path = files[0]
        self.load_subtitle_file(file_path)
        InfoBar.success(
            self.tr("导入成功"),
            self.tr("成功导入") + os.path.basename(file_path),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
            parent=self,
        )
        event.acceptProposedAction()

    def eventFilter(self, obj, event):
        tables = [
            table
            for table in (
                getattr(self, "original_table", None),
                getattr(self, "subtitle_table", None),
            )
            if table is not None
        ]
        drag_targets = set(tables)
        drag_targets.update(table.viewport() for table in tables)
        if obj in drag_targets:
            if event.type() == QEvent.DragEnter:
                self.dragEnterEvent(event)
                return event.isAccepted()
            if event.type() == QEvent.DragMove:
                self.dragMoveEvent(event)
                return event.isAccepted()
            if event.type() == QEvent.Drop:
                self.dropEvent(event)
                return event.isAccepted()
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()
        super().closeEvent(event)

    def show_subtitle_settings(self):
        """显示字幕设置对话框"""
        dialog = SubtitleSettingDialog(self.window())
        dialog.exec_()

    def show_video_player(self):
        """显示视频播放器窗口"""
        # 创建视频播放器窗口
        from ..components.MyVideoWidget import MyVideoWidget

        self.video_player = MyVideoWidget()
        self.video_player.resize(800, 600)

        def signal_update():
            if not self.model._data:
                return
            temp_srt_path = os.path.join(tempfile.gettempdir(), "temp_subtitle.srt")
            asr_data = ASRData.from_json(self.model._data)
            asr_data.save(
                temp_srt_path,
                layout=cfg.subtitle_layout.value,
            )
            signalBus.add_subtitle(temp_srt_path)

        # 如果有字幕文件,则添加字幕
        signal_update()

        signalBus.subtitle_layout_changed.connect(signal_update)
        self.model.dataChanged.connect(signal_update)
        self.model.layoutChanged.connect(signal_update)
        self.original_model.dataChanged.connect(signal_update)
        self.original_model.layoutChanged.connect(signal_update)

        # 如果有关联的视频文件,则自动加载
        if self.task and hasattr(self.task, "file_path") and self.task.file_path:
            self.video_player.setVideo(QUrl.fromLocalFile(self.task.file_path))

        self.video_player.show()
        self.video_player.play()

    def on_subtitle_clicked(self, index):
        row = index.row()
        item = list(self.model._data.values())[row]
        start_time = item["start_time"]  # 毫秒
        end_time = (
            item["end_time"] - 50
            if item["end_time"] - 50 > start_time
            else item["end_time"]
        )
        signalBus.play_video_segment(start_time, end_time)

    def show_context_menu(self, pos):
        """显示右键菜单"""
        menu = RoundMenu(parent=self)
        table = self.sender()
        if table not in {self.original_table, self.subtitle_table}:
            table = self.subtitle_table

        # 获取选中的行
        indexes = table.selectedIndexes()
        if not indexes:
            return

        # 获取唯一的行号
        rows = sorted(set(index.row() for index in indexes))
        if not rows:
            return

        # 添加菜单项
        # retranslate_action = Action(FIF.SYNC, self.tr("重新翻译"))
        merge_action = Action(FIF.LINK, self.tr("合并"))  # 添加快捷键提示
        # menu.addAction(retranslate_action)
        menu.addAction(merge_action)
        merge_action.setShortcut("Ctrl+M")  # 设置快捷键

        # 设置动作状态
        # retranslate_action.setEnabled(cfg.need_translate.value)
        merge_action.setEnabled(len(rows) > 1)

        # 连接动作信号
        # retranslate_action.triggered.connect(lambda: self.retranslate_selected_rows(rows))
        merge_action.triggered.connect(lambda: self.merge_selected_rows(rows))

        # 显示菜单
        menu.exec(table.viewport().mapToGlobal(pos))

    def merge_selected_rows(self, rows):
        """合并选中的字幕行"""
        if not rows or len(rows) < 2:
            return

        # 获取选中行的数据
        data = self.model._data
        data_list = list(data.values())

        # 获取第一行和最后一行的时间戳
        first_row = data_list[rows[0]]
        last_row = data_list[rows[-1]]
        start_time = first_row["start_time"]
        end_time = last_row["end_time"]

        # 合并字幕内容
        original_subtitles = []
        translated_subtitles = []
        for row in rows:
            item = data_list[row]
            original_subtitles.append(item["original_subtitle"])
            translated_subtitles.append(item["translated_subtitle"])

        merged_original = " ".join(original_subtitles)
        merged_translated = " ".join(translated_subtitles)

        # 创建新的合并后的字幕项
        merged_item = {
            "start_time": start_time,
            "end_time": end_time,
            "original_subtitle": merged_original,
            "translated_subtitle": merged_translated,
        }

        # 获取所有需要保留的键
        keys = list(data.keys())
        preserved_keys = keys[: rows[0]] + keys[rows[-1] + 1 :]

        # 创建新的数据字典
        new_data = {}
        for i, key in enumerate(preserved_keys):
            if i == rows[0]:
                new_key = f"{len(new_data)+1}"
                new_data[new_key] = merged_item
            new_key = f"{len(new_data)+1}"
            new_data[new_key] = data[key]

        # 如果合并的是最后几行，需要确保合并项被添加
        if rows[0] >= len(preserved_keys):
            new_key = f"{len(new_data)+1}"
            new_data[new_key] = merged_item

        # 更新模型数据
        self._set_subtitle_data(new_data)

        # 显示成功提示
        InfoBar.success(
            self.tr("合并成功"),
            self.tr("已成功合并选中的字幕行"),
            duration=1000,
            parent=self,
        )

    def keyPressEvent(self, event):
        """处理键盘事件"""
        # 处理 Ctrl+M 快捷键
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_M:
            table = (
                self.original_table
                if self.original_table.hasFocus()
                else self.subtitle_table
            )
            indexes = table.selectedIndexes()
            if indexes:
                rows = sorted(set(index.row() for index in indexes))
                if len(rows) > 1:
                    self.merge_selected_rows(rows)
            event.accept()
        else:
            super().keyPressEvent(event)

    def cancel_optimization(self):
        """取消字幕校正"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()
            self.start_button.setEnabled(True)
            self.cancel_button.hide()
            self.progress_bar.setValue(0)
            self.status_label.setText(self.tr("已取消校正"))
            self.append_task_log(self.tr("任务已取消"))
            InfoBar.warning(
                self.tr("已取消"), self.tr("字幕校正已取消"), duration=3000, parent=self
            )

    def on_target_language_changed(self, language: str):
        """处理翻译语言变更"""
        for lang in TargetLanguageEnum:
            if lang.value == language:
                cfg.set(cfg.target_language, lang)
                self._update_translation_button_state()
                break

    def select_translation_language(self, language: str):
        """从字幕翻译菜单选择目标语言，并启用字幕翻译。"""
        for lang in TargetLanguageEnum:
            if lang.value == language:
                cfg.set(cfg.target_language, lang)
                cfg.set(cfg.need_translate, True)
                self._update_translation_button_state()
                break

    def on_subtitle_optimization_changed(self, checked: bool):
        """处理字幕优化开关变更"""
        cfg.set(cfg.need_optimize, checked)
        self.optimize_button.setChecked(checked)

    def on_subtitle_translation_changed(self, checked: bool):
        """处理字幕翻译开关变更"""
        cfg.set(cfg.need_translate, checked)
        self._update_translation_button_state()

    def _update_translation_button_state(self):
        """同步字幕翻译菜单与按钮标题"""
        enabled = cfg.need_translate.value
        target_language = cfg.target_language.value.value
        self.translation_disabled_action.setChecked(not enabled)
        for action in self.target_language_actions:
            action.setChecked(enabled and action.text() == target_language)
        if enabled:
            accent_color = self._theme_accent_color()
            self.translation_button.setIcon(
                FIF.LANGUAGE.colored(accent_color, accent_color)
            )
            self.translation_button.setText(self.tr("字幕翻译"))
        else:
            self.translation_button.setIcon(FIF.LANGUAGE)
            self.translation_button.setText(self.tr("字幕翻译：关闭"))

    def on_subtitle_layout_changed(self, layout: str):
        """处理字幕排布变更"""
        cfg.set(cfg.subtitle_layout, layout)
        self.layout_button.setText(layout)


class PromptDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.setWindowTitle(self.tr("文稿提示"))
        # 连接按钮点击事件
        self.yesButton.clicked.connect(self.save_prompt)

    def setup_ui(self):
        self.titleLabel = BodyLabel(self.tr("文稿提示"), self)

        # 添加文本编辑框
        self.text_edit = TextEdit(self)
        self.text_edit.setPlaceholderText(
            self.tr(
                "请输入文稿提示（辅助校正字幕和翻译）\n\n"
                "支持以下内容:\n"
                "1. 术语表 - 专业术语、人名、特定词语的修正对照表\n"
                "示例:\n机器学习->Machine Learning\n马斯克->Elon Musk\n打call->应援\n\n"
                "2. 原字幕文稿 - 视频的原有文稿或相关内容\n"
                "示例: 完整的演讲稿、课程讲义等\n\n"
                "3. 修正要求 - 内容相关的具体修正要求\n"
                "示例: 统一人称代词、规范专业术语等\n\n"
                "注意: 使用小型LLM模型时建议控制文稿在1千字内。对于不同字幕文件,请使用与该字幕相关的文稿提示。"
            )
        )
        self.text_edit.setText(cfg.custom_prompt_text.value)

        self.text_edit.setMinimumWidth(420)
        self.text_edit.setMinimumHeight(380)

        # 添加到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.text_edit)
        self.viewLayout.setSpacing(10)

        # 设置按钮文本
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))

    def get_prompt(self):
        return self.text_edit.toPlainText()

    def save_prompt(self):
        # 在点击确定按钮时保存提示文本到配置
        prompt_text = self.text_edit.toPlainText()
        cfg.set(cfg.custom_prompt_text, prompt_text)


class FullScriptDialog(MessageBoxBase):
    def __init__(self, subtitle_data: dict, layout: str, parent=None):
        super().__init__(parent)
        self.subtitle_data = subtitle_data
        self.layout = layout
        self.has_translation = any(
            (segment.get("translated_subtitle") or "").strip()
            for segment in subtitle_data.values()
        )
        self.setup_ui()
        self.setWindowTitle(self.tr("完整台本"))
        self.yesButton.setText(self.tr("复制全文"))
        self.cancelButton.setText(self.tr("关闭"))
        self.yesButton.clicked.connect(self.copy_script)

    @staticmethod
    def _mode_to_slider_value(mode: str) -> int:
        return {"original": 0, "bilingual": 1, "translated": 2}.get(mode, 1)

    @staticmethod
    def _slider_value_to_mode(value: int) -> str:
        return {0: "original", 1: "bilingual", 2: "translated"}.get(
            value, "bilingual"
        )

    def _get_initial_mode(self) -> str:
        if not self.has_translation or self.layout == "仅原文":
            return "original"
        if self.layout in ["仅译文", "单独输出原文和译文"]:
            return "translated"
        return "bilingual"

    def _update_script_text(self):
        mode = self._slider_value_to_mode(self.mode_slider.value())
        self.mode_value_label.setText(
            {
                "original": self.tr("当前: 原文"),
                "bilingual": self.tr("当前: 双语"),
                "translated": self.tr("当前: 译文"),
            }[mode]
        )
        self.text_edit.setPlainText(
            SubtitleInterface._build_full_script_text(
                self.subtitle_data, self.layout, mode
            )
        )

    def setup_ui(self):
        self.titleLabel = BodyLabel(self.tr("完整台本"), self)
        self.mode_value_label = BodyLabel("", self)
        self.mode_value_label.setAlignment(Qt.AlignCenter)
        self.mode_value_label.setWordWrap(True)
        self.mode_slider = QSlider(Qt.Horizontal, self)
        self.mode_slider.setRange(0, 2)
        self.mode_slider.setSingleStep(1)
        self.mode_slider.setPageStep(1)
        self.mode_slider.setTickInterval(1)
        self.mode_slider.setTickPosition(QSlider.TicksBelow)
        self.mode_slider.setValue(self._mode_to_slider_value(self._get_initial_mode()))
        self.mode_slider.valueChanged.connect(self._update_script_text)

        self.original_mode_label = BodyLabel(self.tr("原文"), self)
        self.translated_mode_label = BodyLabel(self.tr("译文"), self)
        self.original_mode_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.translated_mode_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        slider_layout = QHBoxLayout()
        slider_layout.setSpacing(12)
        slider_layout.addWidget(self.original_mode_label)
        slider_layout.addWidget(self.mode_slider, 1)
        slider_layout.addWidget(self.translated_mode_label)

        self.text_edit = TextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setMinimumWidth(760)
        self.text_edit.setMinimumHeight(420)

        self.widget.setMinimumWidth(820)
        self.widget.setMinimumHeight(620)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.mode_value_label)
        self.viewLayout.addLayout(slider_layout)
        self.viewLayout.addWidget(self.text_edit)
        self.viewLayout.setSpacing(10)

        if not self.has_translation:
            self.mode_slider.setEnabled(False)
            self.mode_value_label.setText(self.tr("当前: 原文（未检测到译文）"))
            self.text_edit.setPlainText(
                SubtitleInterface._build_full_script_text(
                    self.subtitle_data, self.layout, "original"
                )
            )
            return

        self._update_script_text()

    def copy_script(self):
        QApplication.clipboard().setText(self.text_edit.toPlainText())
        InfoBar.success(
            self.tr("复制成功"),
            self.tr("完整台本已复制到剪贴板"),
            duration=2000,
            parent=self,
        )


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    window = SubtitleInterface()
    window.show()
    sys.exit(app.exec_())
