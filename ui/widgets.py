import math
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from core.constants import DEFAULT_QUERY_TYPE_LABEL
from core.logger import logger
from core.utils import format_hu_number, format_quantity_auto_unit


class LogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Műveleti napló")
        self.resize(900, 620)

        layout = QVBoxLayout(self)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(logger.text())
        layout.addWidget(self.text)

        row = QHBoxLayout()
        row.addStretch()

        refresh_btn = QPushButton("Frissítés")
        refresh_btn.clicked.connect(self.refresh_log)
        row.addWidget(refresh_btn)

        close_btn = QPushButton("Bezárás")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)

        layout.addLayout(row)

    def refresh_log(self):
        self.text.setPlainText(logger.text())


class MultiSelectComboBox(QComboBox):
    selectionChanged = Signal()

    def __init__(self, placeholder: str = "Összes", parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self._placeholder = placeholder
        self.lineEdit().setText(self._placeholder)

        self._model = QStandardItemModel(self)
        self.setModel(self._model)

        self.view().clicked.connect(self._on_clicked)
        self._update_summary()

    def clear_items(self):
        self._model.clear()
        self._update_summary()

    def add_check_item(self, text: str, checked: bool = False, user_data: Any = None):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        item.setData(Qt.Checked if checked else Qt.Unchecked, Qt.CheckStateRole)
        item.setData(user_data if user_data is not None else text, Qt.UserRole)
        self._model.appendRow(item)
        self._update_summary()

    def checked_values(self) -> List[Any]:
        return [
            self._model.item(i).data(Qt.UserRole)
            for i in range(self._model.rowCount())
            if self._model.item(i).data(Qt.CheckStateRole) == Qt.Checked
        ]

    def checked_texts(self) -> List[str]:
        return [
            self._model.item(i).text()
            for i in range(self._model.rowCount())
            if self._model.item(i).data(Qt.CheckStateRole) == Qt.Checked
        ]

    def set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self._model.rowCount()):
            self._model.item(i).setData(state, Qt.CheckStateRole)
        self._update_summary()
        self.selectionChanged.emit()

    def set_checked_by_user_data(self, allowed):
        allowed_set = set(allowed)
        for i in range(self._model.rowCount()):
            item = self._model.item(i)
            item.setData(
                Qt.Checked if item.data(Qt.UserRole) in allowed_set else Qt.Unchecked,
                Qt.CheckStateRole,
            )
        self._update_summary()
        self.selectionChanged.emit()

    def _on_clicked(self, index: QModelIndex):
        item = self._model.itemFromIndex(index)
        if not item:
            return
        item.setData(
            Qt.Unchecked if item.data(Qt.CheckStateRole) == Qt.Checked else Qt.Checked,
            Qt.CheckStateRole,
        )
        self._update_summary()
        self.selectionChanged.emit()
        self.showPopup()

    def _update_summary(self):
        checked = self.checked_texts()
        if not checked:
            self.lineEdit().setText(self._placeholder)
        elif len(checked) == 1:
            self.lineEdit().setText(checked[0])
        else:
            self.lineEdit().setText(f"{len(checked)} kiválasztva")


class FileDropWidget(QFrame):
    fileDropped = Signal(str)

    def __init__(self, compact: bool = False, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("fileDropWidget")
        self._drag_active = False
        self._compact = compact

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(6)

        self.title = QLabel("Excel fájl behúzása")
        self.title.setObjectName("fileDropTitle")
        self.title.setAlignment(Qt.AlignCenter)

        subtitle_text = "Húzd ide az .xlsx vagy .xls fájlt,\nvagy válaszd ki tallózással."
        if compact:
            subtitle_text = "Húzd ide az .xlsx / .xls fájlt"

        self.subtitle = QLabel(subtitle_text)
        self.subtitle.setObjectName("fileDropSubtitle")
        self.subtitle.setAlignment(Qt.AlignCenter)

        layout.addStretch()
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addStretch()

        if compact:
            self.setMinimumHeight(90)
        else:
            self.setMinimumHeight(160)

        self._refresh_style()

    def _refresh_style(self):
        self.setProperty("dragActive", self._drag_active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _is_valid_file(self, path: str) -> bool:
        return Path(path).suffix.lower() in {".xlsx", ".xls"}

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile()
                if path and self._is_valid_file(path):
                    self._drag_active = True
                    self._refresh_style()
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._drag_active = False
        self._refresh_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self._drag_active = False
        self._refresh_style()

        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile()
                if path and self._is_valid_file(path):
                    self.fileDropped.emit(path)
                    event.acceptProposedAction()
                    return
        event.ignore()


class ImportDatabaseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Új adatbázis tallózása")
        self.resize(620, 360)

        self.selected_file = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.drop_widget = FileDropWidget(compact=False)
        self.drop_widget.fileDropped.connect(self.set_selected_file)
        layout.addWidget(self.drop_widget)

        file_row = QHBoxLayout()
        self.file_label = QLabel("Nincs fájl kiválasztva")
        self.file_label.setWordWrap(True)
        browse_btn = QPushButton("Tallózás")
        browse_btn.clicked.connect(self.browse_file)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        form = QFormLayout()

        self.query_type_combo = QComboBox()
        self.query_type_combo.addItems(["Keletkezés", "Átvétel", "Átadás", "Mindhárom"])
        self.query_type_combo.setCurrentText(DEFAULT_QUERY_TYPE_LABEL)

        self.cache_chk = QCheckBox("Gyorsítótár használata")
        self.cache_chk.setChecked(True)

        form.addRow("Lekérdezés típusa", self.query_type_combo)
        form.addRow("Beállítás", self.cache_chk)
        layout.addLayout(form)

        self.buttons = QDialogButtonBox()
        self.start_btn = self.buttons.addButton("Indítás", QDialogButtonBox.AcceptRole)
        self.cancel_btn = self.buttons.addButton("Mégse", QDialogButtonBox.RejectRole)

        self.start_btn.clicked.connect(self.validate_and_accept)
        self.cancel_btn.clicked.connect(self.reject)

        layout.addWidget(self.buttons)

    def set_selected_file(self, path: str):
        self.selected_file = path
        self.file_label.setText(path)

    def browse_file(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Excel kiválasztása", "", "Excel Files (*.xlsx *.xls)")
        if path:
            self.set_selected_file(path)

    def validate_and_accept(self):
        if not self.selected_file:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Hiányzó fájl", "Válassz ki vagy húzz be egy Excel fájlt.")
            return
        self.accept()

    def payload(self):
        return {
            "file_path": self.selected_file,
            "query_type": self.query_type_combo.currentText(),
            "use_cache": self.cache_chk.isChecked(),
        }


class HoverAnnotation:
    def __init__(self, ax):
        self.annot = ax.annotate(
            "",
            xy=(0, 0),
            xytext=(12, 12),
            textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.4", fc="#ffffff", ec="#cbd5e1", alpha=0.98),
            arrowprops=dict(arrowstyle="->", color="#64748b", lw=0.8),
            fontsize=9,
            zorder=20,
        )
        self.annot.set_visible(False)

    def show(self, x, y, text: str, x_offset=12, y_offset=12):
        self.annot.xy = (x, y)
        self.annot.set_text(text)
        self.annot.set_position((x_offset, y_offset))
        self.annot.set_visible(True)

    def hide(self):
        self.annot.set_visible(False)


class MplCanvas(FigureCanvas):
    doubleClicked = Signal()

    def __init__(self, width=6, height=4, dpi=100):
        self.figure = Figure(figsize=(width, height), dpi=dpi, facecolor="white")
        self.ax = self.figure.add_subplot(111)
        super().__init__(self.figure)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.hover_targets = []
        self.hover_annotation = HoverAnnotation(self.ax)

        self.mpl_connect("motion_notify_event", self.on_hover)
        self.mpl_connect("button_press_event", self.on_click)
        self.mpl_connect("scroll_event", self.on_scroll)

    def reset_hover(self):
        self.hover_targets = []
        self.hover_annotation.hide()
        self.hover_annotation = HoverAnnotation(self.ax)

    def on_click(self, event):
        if getattr(event, "dblclick", False):
            self.doubleClicked.emit()

    def add_bar_targets(self, bars, texts, values, horizontal=False, percents=None):
        for idx, bar in enumerate(bars):
            if horizontal:
                x = bar.get_width() + getattr(bar, "_x0", 0)
                y = bar.get_y() + bar.get_height() / 2
            else:
                x = bar.get_x() + bar.get_width() / 2
                y = bar.get_y() + bar.get_height()

            text = texts[idx]
            if percents is not None:
                text += f"\nArány: {format_hu_number(percents[idx], 2)} %"
            text += f"\nMennyiség: {format_quantity_auto_unit(values[idx], 2)}"

            self.hover_targets.append({
                "artist": bar,
                "point": (x, y),
                "text": text,
                "kind": "bar",
            })

    def add_point_targets(self, xs, ys, texts):
        for i in range(len(xs)):
            self.hover_targets.append({
                "artist": None,
                "point": (xs[i], ys[i]),
                "text": texts[i],
                "kind": "point",
                "radius_x": 0.35,
                "radius_y": max(abs(ys[i]) * 0.08, 1),
            })

    def add_pie_targets(self, wedges, texts, values, percents=None):
        for idx, wedge in enumerate(wedges):
            angle = (wedge.theta1 + wedge.theta2) / 2
            x = math.cos(math.radians(angle)) * 0.7
            y = math.sin(math.radians(angle)) * 0.7

            text = texts[idx]
            if percents is not None:
                text += f"\nArány: {format_hu_number(percents[idx], 2)} %"
            text += f"\nMennyiség: {format_quantity_auto_unit(values[idx], 2)}"

            self.hover_targets.append({
                "artist": wedge,
                "point": (x, y),
                "text": text,
                "kind": "pie",
            })

    def _tooltip_offsets(self, event):
        if event.x is None or event.y is None:
            return 12, 12
        w = self.width()
        h = self.height()
        x_offset = 12 if event.x < w * 0.7 else -200
        y_offset = 12 if event.y > h * 0.25 else 40
        return x_offset, y_offset

    def on_hover(self, event):
        if event.inaxes != self.ax:
            if self.hover_annotation.annot.get_visible():
                self.hover_annotation.hide()
                self.draw_idle()
            return

        shown = False
        x_off, y_off = self._tooltip_offsets(event)

        for target in self.hover_targets:
            kind = target.get("kind", "bar")
            if kind == "point":
                if event.xdata is None or event.ydata is None:
                    continue
                px, py = target["point"]
                if abs(event.xdata - px) <= target.get("radius_x", 0.3) and abs(event.ydata - py) <= target.get("radius_y", 1.0):
                    self.hover_annotation.show(px, py, target["text"], x_off, y_off)
                    shown = True
                    break
            else:
                try:
                    contains, _ = target["artist"].contains(event)
                except Exception:
                    contains = False
                if contains:
                    px, py = target["point"]
                    self.hover_annotation.show(px, py, target["text"], x_off, y_off)
                    shown = True
                    break

        if not shown:
            self.hover_annotation.hide()

        self.draw_idle()

    def on_scroll(self, event):
        if event.inaxes != self.ax:
            return

        base_scale = 1.15
        cur_xlim = self.ax.get_xlim()
        cur_ylim = self.ax.get_ylim()

        xdata = event.xdata if event.xdata is not None else (cur_xlim[0] + cur_xlim[1]) / 2
        ydata = event.ydata if event.ydata is not None else (cur_ylim[0] + cur_ylim[1]) / 2

        scale_factor = 1 / base_scale if event.button == "up" else base_scale
        new_width = (cur_xlim[1] - cur_xlim[0]) * scale_factor
        new_height = (cur_ylim[1] - cur_ylim[0]) * scale_factor

        relx = (cur_xlim[1] - xdata) / (cur_xlim[1] - cur_xlim[0]) if (cur_xlim[1] - cur_xlim[0]) else 0.5
        rely = (cur_ylim[1] - ydata) / (cur_ylim[1] - cur_ylim[0]) if (cur_ylim[1] - cur_ylim[0]) else 0.5

        self.ax.set_xlim([xdata - new_width * (1 - relx), xdata + new_width * relx])
        self.ax.set_ylim([ydata - new_height * (1 - rely), ydata + new_height * rely])
        self.draw_idle()


class FullscreenChartDialog(QDialog):
    def __init__(self, title: str, render_callback: Callable[["MplCanvas"], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1400, 900)

        layout = QVBoxLayout(self)
        self.canvas = MplCanvas(width=12, height=8)
        layout.addWidget(self.canvas, 1)

        row = QHBoxLayout()
        row.addStretch()
        close_btn = QPushButton("Bezárás")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

        render_callback(self.canvas)


class DashboardCard(QFrame):
    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("dashboardCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("dashboardCardTitle")
        layout.addWidget(self.title_label)

        self.value_label = QLabel("-")
        self.value_label.setObjectName("dashboardCardValue")
        layout.addWidget(self.value_label)

        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("dashboardCardSubtitle")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.subtitle_label)

    def set_value(self, value: str):
        self.value_label.setText(value)


class ChartPanel(QFrame):
    def __init__(self, title: str, expand_callback: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self.expand_callback = expand_callback
        self.setObjectName("chartPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("chartPanelTitle")
        header.addWidget(self.title_label)
        header.addStretch()

        self.expand_btn = QPushButton("⤢")
        self.expand_btn.setFixedSize(34, 26)
        self.expand_btn.clicked.connect(self.on_expand)
        header.addWidget(self.expand_btn)

        layout.addLayout(header)

        self.canvas = MplCanvas(width=8, height=5)
        self.canvas.doubleClicked.connect(self.on_expand)
        layout.addWidget(self.canvas, 1)

    def on_expand(self):
        if self.expand_callback:
            self.expand_callback()