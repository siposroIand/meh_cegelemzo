import pandas as pd
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from core.constants import (
    CACHE_DB,
    DEFAULT_QUERY_TYPE_LABEL,
    ENDPOINTS,
    FILTER_DEBOUNCE_MS,
    LABEL_TO_ADATTIPUS,
    OUTPUT_DIR,
    PRODUCT_GROUPS_JSON,
)
from core.logger import logger
from core.utils import (
    axis_int_formatter,
    axis_number_formatter,
    convert_for_axis,
    format_hu_number,
    format_phone,
    format_quantity_auto_unit,
    parse_hu_numeric,
    safe_str,
    split_address_parts,
)
from services.aggregation_service import aggregate_data
from services.cache_db import CacheDB
from services.category_service import CategoryService
from services.export_service import (
    build_detail_export,
    build_detail_pivot_export,
    build_outline_report_sheet,
    build_summary_export,
    style_excel_worksheet,
)
from services.import_service import build_source_from_excel_by_adoszam
from services.okir_client import OkirClient
from services.persistence_service import PersistenceService
from ui.widgets import (
    ChartPanel,
    DashboardCard,
    FullscreenChartDialog,
    ImportDatabaseDialog,
    LogDialog,
    MultiSelectComboBox,
)
from workers.fetch_worker import FetchWorker


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MÉH Zrt. - Cégelemző")
        self.resize(1600, 980)
        self.showMaximized()

        self.source_df = pd.DataFrame()
        self.raw_df = pd.DataFrame()
        self.missing_df = pd.DataFrame()

        self.category_service = CategoryService(PRODUCT_GROUPS_JSON)
        self.agg = aggregate_data(pd.DataFrame(), pd.DataFrame(), self.category_service)
        self.max_data_year = None

        self.cache = CacheDB(CACHE_DB)
        self.persistence = PersistenceService(CACHE_DB)
        self.ktj_meta_cache = {}

        self.current_file_path = ""
        self.current_query_label = DEFAULT_QUERY_TYPE_LABEL
        self.current_use_cache = True

        self._build_ui()
        self._build_menu()

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self.refresh_all_views)

        self._apply_styles()
        self.set_status("Készen áll.")

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def set_status(self, text: str, progress_value=None, progress_max=None, log=False):
        self.status_label.setText(text)
        if progress_max is not None:
            self.progress.setMaximum(max(progress_max, 1))
        if progress_value is not None:
            self.progress.setValue(progress_value)

        if log:
            try:
                logger.write(text)
            except Exception:
                pass

    def selected_types_from_label(self, label: str):
        return list(ENDPOINTS.keys()) if label == "Mindhárom" else [LABEL_TO_ADATTIPUS[label]]

    def shorten_label(self, text: str, max_len: int = 20) -> str:
        s = str(text or "").strip()
        return s if len(s) <= max_len else s[: max_len - 1] + "…"

    def clean_int_like(self, value):
        if value is None:
            return ""

        s = str(value).strip()
        if not s or s.lower() == "nan":
            return ""

        try:
            f = float(s.replace(",", "."))
            if f.is_integer():
                return str(int(f))
        except Exception:
            pass

        return s

    def parse_quantity_value(self, value):
        if value is None:
            return None

        if isinstance(value, (int, float)):
            try:
                if pd.isna(value):
                    return None
            except Exception:
                pass
            return float(value)

        s = str(value).strip()
        if not s:
            return None

        s = s.replace("\xa0", "").replace(" ", "")

        try:
            if "," in s and "." in s:
                s = s.replace(".", "").replace(",", ".")
            elif "," in s:
                s = s.replace(",", ".")
            return float(s)
        except Exception:
            return None

    def get_quantity_series(self, df: pd.DataFrame):
        if df is None or df.empty:
            return pd.Series(dtype="float64")

        qty_col = None
        if "OSSZES_MENNYISEG" in df.columns:
            qty_col = "OSSZES_MENNYISEG"
        elif "MENNYISEG" in df.columns:
            qty_col = "MENNYISEG"

        if not qty_col:
            return pd.Series([None] * len(df), index=df.index, dtype="float64")

        return df[qty_col].apply(self.parse_quantity_value).astype("float64")

    def add_quantity_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df

        out = df.copy()
        qty_kg = self.get_quantity_series(out)

        out["Mennyiség (kg)"] = qty_kg
        out["Mennyiség (t)"] = qty_kg / 1000.0

        return out

    def resolve_product_group(self, code):
        code_str = str(code).strip()

        for method_name in [
            "categorize_code",
            "categorize",
            "get_group_for_code",
            "group_for_code",
            "resolve_group",
        ]:
            method = getattr(self.category_service, method_name, None)
            if callable(method):
                try:
                    result = method(code_str)
                    if result:
                        return str(result)
                except Exception:
                    pass

        for attr_name in ["code_to_group", "_code_to_group", "group_by_code", "_group_by_code"]:
            mapping = getattr(self.category_service, attr_name, None)
            if isinstance(mapping, dict):
                return str(mapping.get(code_str, "Nem besorolható"))

        return "Nem besorolható"

    def ensure_ktj_metadata_loaded(self, df: pd.DataFrame):
        if df is None or df.empty or "KTJ" not in df.columns:
            return

        unique_ktjs = []
        for val in df["KTJ"].dropna().tolist():
            ktj = self.clean_int_like(val)
            if ktj and ktj not in unique_ktjs:
                unique_ktjs.append(ktj)

        if not unique_ktjs:
            return

        client = OkirClient()

        for ktj in unique_ktjs:
            if ktj in self.ktj_meta_cache:
                continue

            cached = None
            try:
                cached = self.persistence.load_ktj_meta(ktj)
            except Exception:
                cached = None

            if cached is not None:
                self.ktj_meta_cache[ktj] = cached
                continue

            try:
                payload = client.fetch_first_ktj_detail(ktj) or {}
                self.ktj_meta_cache[ktj] = payload
                try:
                    self.persistence.save_ktj_meta(ktj, payload)
                except Exception:
                    pass
            except Exception:
                self.ktj_meta_cache[ktj] = {}

    def enrich_with_ktj_metadata(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df

        out = df.copy()

        if "KTJ" not in out.columns:
            if "SOURCE_KTJ" in out.columns:
                out["KTJ"] = out["SOURCE_KTJ"]
            else:
                out["KTJ"] = ""

        out["KTJ"] = out["KTJ"].apply(self.clean_int_like)

        def meta_value(ktj_value, key):
            ktj = self.clean_int_like(ktj_value)
            item = self.ktj_meta_cache.get(ktj, {})
            if isinstance(item, dict):
                return item.get(key, "")
            return ""

        out["KTJ megnevezés"] = out["KTJ"].apply(lambda x: meta_value(x, "MEGNEVEZES"))
        out["Besorolás"] = out["KTJ"].apply(lambda x: meta_value(x, "BESOROLAS"))
        out["Irányítószám"] = out["KTJ"].apply(lambda x: self.clean_int_like(meta_value(x, "IRSZAM")))
        out["Helység"] = out["KTJ"].apply(lambda x: meta_value(x, "HELYSEG"))
        out["Cím"] = out["KTJ"].apply(lambda x: meta_value(x, "CIM"))
        out["Megye"] = out["KTJ"].apply(lambda x: meta_value(x, "MEGYE_NEV"))
        out["Régió"] = out["KTJ"].apply(lambda x: meta_value(x, "REGIO_NEV"))

        return out

    def capture_state(self):
        return {
            "version": 1,
            "current_file_path": self.current_file_path,
            "current_query_label": self.current_query_label,
            "current_use_cache": self.current_use_cache,
            "selected_companies": list(self.filter_company.checked_values()),
            "selected_years": list(self.filter_year.checked_values()),
            "selected_categories": list(self.filter_category.checked_values()),
            "filter_code": self.filter_code.text().strip(),
            "current_tab_index": self.tabs.currentIndex(),
        }

    def restore_state(self, state: dict):
        if not state:
            return

        self.current_file_path = state.get("current_file_path", "")
        self.current_query_label = state.get("current_query_label", DEFAULT_QUERY_TYPE_LABEL)
        self.current_use_cache = bool(state.get("current_use_cache", True))

        self.current_file_label.setText(self.current_file_path or "-")
        self.current_type_label.setText(self.current_query_label or "-")
        self.current_cache_label.setText("Igen" if self.current_use_cache else "Nem")

        self.populate_filters()

        companies = set(str(x) for x in state.get("selected_companies", []))
        years = set(int(x) for x in state.get("selected_years", []))
        categories = set(str(x) for x in state.get("selected_categories", []))

        try:
            self.filter_company.blockSignals(True)
            self.filter_year.blockSignals(True)
            self.filter_category.blockSignals(True)

            self.filter_company.set_checked_by_user_data(companies)
            self.filter_year.set_checked_by_user_data(years)
            self.filter_category.set_checked_by_user_data(categories)
        finally:
            self.filter_company.blockSignals(False)
            self.filter_year.blockSignals(False)
            self.filter_category.blockSignals(False)

        self.filter_code.setText(state.get("filter_code", ""))
        self.refresh_all_views()

        idx = int(state.get("current_tab_index", 0))
        idx = max(0, min(idx, self.tabs.count() - 1))
        self.tabs.setCurrentIndex(idx)

    # -------------------------------------------------------------------------
    # Menu
    # -------------------------------------------------------------------------
    def _build_menu(self):
        menubar = self.menuBar()

        import_menu = menubar.addMenu("Importálás")

        load_snapshot_action = QAction("Betöltés helyi mentésből", self)
        load_snapshot_action.triggered.connect(self.load_snapshot)
        import_menu.addAction(load_snapshot_action)

        browse_new_action = QAction("Új adatbázis tallózása", self)
        browse_new_action.triggered.connect(self.open_import_dialog)
        import_menu.addAction(browse_new_action)

        import_package_action = QAction("Megosztott csomag betöltése", self)
        import_package_action.triggered.connect(self.import_shared_package)
        import_menu.addAction(import_package_action)

        export_menu = menubar.addMenu("Exportálás")

        save_snapshot_action = QAction("Helyi mentés készítése", self)
        save_snapshot_action.triggered.connect(self.save_snapshot)
        export_menu.addAction(save_snapshot_action)

        export_summary_action = QAction("Excel összesítő (termékcsoportonként)", self)
        export_summary_action.triggered.connect(self.export_summary_excel)
        export_menu.addAction(export_summary_action)

        export_detail_action = QAction("Excel részletes (HAK szerint)", self)
        export_detail_action.triggered.connect(self.export_detail_excel)
        export_menu.addAction(export_detail_action)

        export_ktj_action = QAction("Excel export (KTJ szerint)", self)
        export_ktj_action.triggered.connect(self.export_ktj_excel)
        export_menu.addAction(export_ktj_action)

        export_package_action = QAction("Megosztott csomag készítése", self)
        export_package_action.triggered.connect(self.export_shared_package)
        export_menu.addAction(export_package_action)

        program_menu = menubar.addMenu("Program")

        clear_cache_action = QAction("Gyorsítótár törlése", self)
        clear_cache_action.triggered.connect(self.clear_cache)
        program_menu.addAction(clear_cache_action)

        log_action = QAction("Műveleti napló", self)
        log_action.triggered.connect(lambda: LogDialog(self).exec())
        program_menu.addAction(log_action)

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        status_wrap = QHBoxLayout()
        self.status_label = QLabel("Készen áll.")
        self.progress = QProgressBar()
        self.progress.setMaximumHeight(14)
        self.progress.setTextVisible(False)
        status_wrap.addWidget(self.status_label, 2)
        status_wrap.addWidget(self.progress, 3)
        root.addLayout(status_wrap)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        source_group = QGroupBox("Aktuális lekérdezés")
        source_form = QFormLayout(source_group)

        self.current_file_label = QLabel("-")
        self.current_file_label.setWordWrap(True)
        self.current_type_label = QLabel("-")
        self.current_cache_label = QLabel("-")

        source_form.addRow("Fájl", self.current_file_label)
        source_form.addRow("Lekérdezés típusa", self.current_type_label)
        source_form.addRow("Gyorsítótár", self.current_cache_label)

        filter_group = QGroupBox("Szűrők")
        form = QFormLayout(filter_group)

        self.filter_company = MultiSelectComboBox("Összes")
        self.filter_year = MultiSelectComboBox("Összes")
        self.filter_category = MultiSelectComboBox("Összes")
        self.filter_code = QLineEdit()
        self.filter_code.setPlaceholderText("pl. 170401;170402")

        self.filter_company.selectionChanged.connect(self.apply_filters)
        self.filter_year.selectionChanged.connect(self.apply_filters)
        self.filter_category.selectionChanged.connect(self.apply_filters)
        self.filter_code.textChanged.connect(self.apply_filters)

        form.addRow("Cég", self.filter_company)
        form.addRow("Év", self.filter_year)
        form.addRow("Termékcsoport", self.filter_category)
        form.addRow("HAK", self.filter_code)

        reset_btn = QPushButton("Szűrők törlése")
        reset_btn.clicked.connect(self.reset_filters)
        form.addRow("", reset_btn)

        self.exec_text = QTextEdit()
        self.exec_text.setReadOnly(True)

        self.company_info = QTextEdit()
        self.company_info.setReadOnly(True)

        left_layout.addWidget(source_group)
        left_layout.addWidget(filter_group)
        left_layout.addWidget(QLabel("Összefoglaló"))
        left_layout.addWidget(self.exec_text, 1)
        left_layout.addWidget(QLabel("Cégadatok"))
        left_layout.addWidget(self.company_info, 1)

        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tab_dashboard = QWidget()
        self.tab_data = QWidget()
        self.tab_ktj = QWidget()
        self.tab_missing = QWidget()

        self.tabs.addTab(self.tab_dashboard, "Dashboard")
        self.tabs.addTab(self.tab_data, "Adatok")
        self.tabs.addTab(self.tab_ktj, "KTJ szerint")
        self.tabs.addTab(self.tab_missing, "Kimaradt")
        self.tabs.currentChanged.connect(self.on_tab_changed)

        right_layout.addWidget(self.tabs)
        splitter.addWidget(right)
        splitter.setSizes([320, 1280])

        self._build_dashboard_tab()
        self._build_data_tab()
        self._build_ktj_tab()
        self._build_missing_tab()

    def _apply_styles(self):
        self.setStyleSheet("""
            QWidget {
                background: #f8fafc;
                color: #0f172a;
                font-size: 13px;
            }
            QGroupBox, #dashboardCard, #chartPanel, #fileDropWidget {
                background: white;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
            }
            QGroupBox {
                margin-top: 10px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: #334155;
                font-weight: 600;
            }
            QPushButton {
                background: white;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 6px 10px;
            }
            QPushButton:hover {
                background: #f1f5f9;
            }
            QLineEdit, QComboBox, QTextEdit, QTabWidget::pane, QTableWidget {
                background: white;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
            }
            #dashboardCardTitle {
                color: #64748b;
                font-size: 12px;
                font-weight: 600;
            }
            #dashboardCardValue {
                color: #0f172a;
                font-size: 22px;
                font-weight: 700;
            }
            #dashboardCardSubtitle {
                color: #64748b;
                font-size: 11px;
            }
            #chartPanelTitle {
                font-size: 14px;
                font-weight: 700;
                color: #0f172a;
            }
            #fileDropTitle {
                font-size: 16px;
                font-weight: 700;
                color: #0f172a;
            }
            #fileDropSubtitle {
                color: #64748b;
                font-size: 12px;
            }
            #fileDropWidget[dragActive="true"] {
                border: 2px dashed #2563eb;
                background: #eff6ff;
            }
        """)

    def _build_dashboard_tab(self):
        root = QVBoxLayout(self.tab_dashboard)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        kpi_row = QHBoxLayout()
        self.kpi_total = DashboardCard("Összes mennyiség", "Valós összesített mennyiség")
        self.kpi_companies = DashboardCard("Cégek száma", "Szűrt eredmény")
        self.kpi_codes = DashboardCard("Hulladékkódok", "Egyedi HAK")
        self.kpi_danger = DashboardCard("Veszélyességi arány", "Összmennyiség alapján")
        self.kpi_years = DashboardCard("Vizsgált évek", "Aktuális szűrés")

        for card in [self.kpi_total, self.kpi_companies, self.kpi_codes, self.kpi_danger, self.kpi_years]:
            kpi_row.addWidget(card)

        root.addLayout(kpi_row)

        middle = QHBoxLayout()
        self.panel_yearly = ChartPanel("Éves trend", lambda: self.open_chart("Éves trend", self.render_yearly_chart))
        middle.addWidget(self.panel_yearly, 2)

        right_stack = QVBoxLayout()
        self.panel_danger = ChartPanel("Veszélyességi megoszlás", lambda: self.open_chart("Veszélyességi megoszlás", self.render_danger_chart))
        self.panel_finance = ChartPanel("Árbevétel / saját tőke", lambda: self.open_chart("Árbevétel / saját tőke", self.render_finance_chart))
        right_stack.addWidget(self.panel_danger, 1)
        right_stack.addWidget(self.panel_finance, 1)
        middle.addLayout(right_stack, 1)

        root.addLayout(middle, 2)

        bottom = QHBoxLayout()
        self.panel_company = ChartPanel("Top cégek összetétele", lambda: self.open_chart("Top cégek összetétele", self.render_company_chart))
        self.panel_category = ChartPanel("Termékcsoportok megoszlása", lambda: self.open_chart("Termékcsoportok megoszlása", self.render_category_chart))
        bottom.addWidget(self.panel_company, 1)
        bottom.addWidget(self.panel_category, 1)
        root.addLayout(bottom, 2)

    def _build_data_tab(self):
        layout = QVBoxLayout(self.tab_data)
        self.data_year_tabs = QTabWidget()
        layout.addWidget(self.data_year_tabs)

    def _build_ktj_tab(self):
        layout = QVBoxLayout(self.tab_ktj)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.ktj_table = QTableWidget()
        self.configure_table(self.ktj_table)
        layout.addWidget(self.ktj_table)

    def _build_missing_tab(self):
        layout = QVBoxLayout(self.tab_missing)
        self.missing_table = QTableWidget()
        self.configure_table(self.missing_table)
        layout.addWidget(self.missing_table)

    def configure_table(self, table: QTableWidget):
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)

    # -------------------------------------------------------------------------
    # Import flow
    # -------------------------------------------------------------------------
    def open_import_dialog(self):
        dlg = ImportDatabaseDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        payload = dlg.payload()
        self.current_file_path = payload["file_path"]
        self.current_query_label = payload["query_type"]
        self.current_use_cache = payload["use_cache"]

        self.current_file_label.setText(self.current_file_path)
        self.current_type_label.setText(self.current_query_label)
        self.current_cache_label.setText("Igen" if self.current_use_cache else "Nem")

        self.start_fetch()

    # -------------------------------------------------------------------------
    # Fetch
    # -------------------------------------------------------------------------
    def start_fetch(self):
        file_path = self.current_file_path.strip()
        if not file_path:
            QMessageBox.warning(self, "Hiányzó fájl", "Előbb válassz ki egy Excel fájlt.")
            return

        try:
            self.set_status("Forrásfájl beolvasása...")
            self.progress.setRange(0, 0)
            self.repaint()

            client = OkirClient()
            self.source_df = build_source_from_excel_by_adoszam(
                file_path,
                client,
                log_fn=logger.write,
                progress_fn=self.on_source_progress,
            )

            if self.source_df.empty:
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                QMessageBox.warning(self, "Nincs adat", "Nem sikerült forrásadatot előállítani.")
                self.set_status("A forrásfájl feldolgozása nem adott eredményt.")
                return
        except Exception as e:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.set_status(f"Hiba történt: {e}")
            QMessageBox.critical(self, "Hiba", str(e))
            return

        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.set_status("Hulladék adatok lekérése indul...")

        self.worker = FetchWorker(
            self.source_df.copy(),
            self.current_use_cache,
            False,
            self.selected_types_from_label(self.current_query_label),
            max_workers=4,
        )
        self.worker.progress_signal.connect(self.on_progress)
        self.worker.finished_signal.connect(self.on_fetch_finished)
        self.worker.error_signal.connect(self.on_fetch_error)
        self.worker.start()

    def on_source_progress(self, step, total, text):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(step)
        self.status_label.setText(text)

    def on_progress(self, step, total, text):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(step)
        self.status_label.setText(text)

    def on_fetch_finished(self, source_df, raw_df, missing_df, max_year):
        self.source_df = source_df
        self.raw_df = raw_df
        self.missing_df = missing_df
        self.max_data_year = max_year

        self.set_status("Adatok aggregálása...", 90, 100)
        self.agg = aggregate_data(self.raw_df, self.source_df, self.category_service)

        self.set_status("Szűrők frissítése...", 95, 100)
        self.populate_filters()
        self.apply_default_year_selection()

        self.set_status("Nézetek frissítése...", 98, 100)
        self.refresh_all_views()

        self.set_status("Lekérdezés kész.", 100, 100)
        QMessageBox.information(self, "Kész", "Az adatok lekérése befejeződött.")

    def on_fetch_error(self, err):
        self.set_status(f"Hiba: {err}", 0, 100)
        QMessageBox.critical(self, "Hiba", err)

    # -------------------------------------------------------------------------
    # Filters
    # -------------------------------------------------------------------------
    def populate_filters(self):
        master = self.agg.master

        for combo in [self.filter_company, self.filter_year, self.filter_category]:
            combo.blockSignals(True)
            combo.clear_items()
            combo.blockSignals(False)

        if master is None or master.empty:
            return

        for combo in [self.filter_company, self.filter_year, self.filter_category]:
            combo.blockSignals(True)

        try:
            if "CEGNEV" in master.columns:
                for c in sorted(master["CEGNEV"].dropna().astype(str).unique()):
                    self.filter_company.add_check_item(c, False, c)

            if "EV" in master.columns:
                for y in sorted(master["EV"].dropna().astype(int).unique(), reverse=True):
                    self.filter_year.add_check_item(str(y), False, int(y))

            if "PRODUCT_GROUP" in master.columns:
                for g in sorted(master["PRODUCT_GROUP"].dropna().astype(str).unique()):
                    self.filter_category.add_check_item(g, False, g)
        finally:
            for combo in [self.filter_company, self.filter_year, self.filter_category]:
                combo.blockSignals(False)

    def apply_default_year_selection(self):
        master = self.agg.master
        if master is None or master.empty or "EV" not in master.columns:
            return
        years = sorted(master["EV"].dropna().astype(int).unique(), reverse=True)
        if years:
            self.filter_year.set_checked_by_user_data(set(years[:2]))

    def apply_filters(self):
        self._filter_timer.start(FILTER_DEBOUNCE_MS)

    def reset_filters(self):
        for combo in [self.filter_company, self.filter_year, self.filter_category]:
            combo.blockSignals(True)
            combo.set_all_checked(False)
            combo.blockSignals(False)

        self.filter_code.blockSignals(True)
        self.filter_code.clear()
        self.filter_code.blockSignals(False)

        self.apply_default_year_selection()
        self.set_status("Szűrők alaphelyzetbe állítva.")
        self.refresh_all_views()

    def get_filtered_master(self):
        master = self.agg.master
        if master is None or master.empty:
            return master

        df = master
        selected_companies = self.filter_company.checked_values()
        selected_years = self.filter_year.checked_values()
        selected_categories = self.filter_category.checked_values()
        code_text = self.filter_code.text().strip()

        if selected_companies and "CEGNEV" in df.columns:
            df = df[df["CEGNEV"].astype(str).isin([str(x) for x in selected_companies])]

        if selected_years and "EV" in df.columns:
            df = df[df["EV"].astype("Int64").isin([int(x) for x in selected_years])]

        if selected_categories and "PRODUCT_GROUP" in df.columns:
            df = df[df["PRODUCT_GROUP"].astype(str).isin([str(x) for x in selected_categories])]

        if code_text and "HULLADEKKOD" in df.columns:
            codes = [c.strip() for c in code_text.split(";") if c.strip()]
            if codes:
                pattern = "|".join(codes)
                df = df[df["HULLADEKKOD"].astype(str).str.contains(pattern, case=False, na=False, regex=True)]

        return df

    def get_filtered_raw(self):
        if self.raw_df is None or self.raw_df.empty:
            return pd.DataFrame()

        df = self.raw_df.copy()

        selected_companies = self.filter_company.checked_values()
        selected_years = self.filter_year.checked_values()
        selected_categories = self.filter_category.checked_values()
        code_text = self.filter_code.text().strip()

        if selected_companies and "CEGNEV" in df.columns:
            df = df[df["CEGNEV"].astype(str).isin([str(x) for x in selected_companies])]

        if selected_years and "EV" in df.columns:
            df = df[df["EV"].astype("Int64").isin([int(x) for x in selected_years])]

        if code_text and "HULLADEKKOD" in df.columns:
            codes = [c.strip() for c in code_text.split(";") if c.strip()]
            if codes:
                pattern = "|".join(codes)
                df = df[df["HULLADEKKOD"].astype(str).str.contains(pattern, case=False, na=False, regex=True)]

        if selected_categories and "HULLADEKKOD" in df.columns:
            df = df.copy()
            df["PRODUCT_GROUP_TMP"] = df["HULLADEKKOD"].astype(str).apply(self.resolve_product_group)
            df = df[df["PRODUCT_GROUP_TMP"].isin([str(x) for x in selected_categories])]
            df.drop(columns=["PRODUCT_GROUP_TMP"], inplace=True, errors="ignore")

        return df

    def get_filtered_base_unique(self):
        master = self.get_filtered_master()
        if master is None or master.empty:
            return pd.DataFrame()

        cols = [
            "CEGNEV",
            "SOURCE_KUJ",
            "EV",
            "HULLADEKKOD",
            "ADATTIPUS",
            "ADATTIPUS_LABEL",
            "OSSZES_MENNYISEG",
            "HULLADEK_VESZELYES_10",
        ]
        existing_cols = [c for c in cols if c in master.columns]
        if not existing_cols:
            return pd.DataFrame()

        return master[existing_cols].drop_duplicates().copy()

    # -------------------------------------------------------------------------
    # Refresh
    # -------------------------------------------------------------------------
    def refresh_all_views(self):
        self.refresh_dashboard()
        self.refresh_data_year_tabs()
        self.refresh_missing_table()
        self.refresh_exec_summary()
        self.refresh_company_info()

    def on_tab_changed(self, idx):
        if idx == 0:
            self.refresh_dashboard()
        elif idx == 2:
            self.refresh_ktj_tab()

    def refresh_dashboard(self):
        self.refresh_kpis()
        self.render_yearly_chart(self.panel_yearly.canvas)
        self.render_danger_chart(self.panel_danger.canvas)
        self.render_finance_chart(self.panel_finance.canvas)
        self.render_company_chart(self.panel_company.canvas)
        self.render_category_chart(self.panel_category.canvas)

    def refresh_kpis(self):
        master = self.get_filtered_master()
        base = self.get_filtered_base_unique()

        if master is None or master.empty or base.empty or "OSSZES_MENNYISEG" not in base.columns:
            self.kpi_total.set_value("-")
            self.kpi_companies.set_value("-")
            self.kpi_codes.set_value("-")
            self.kpi_danger.set_value("-")
            self.kpi_years.set_value("-")
            return

        total_qty = pd.to_numeric(base["OSSZES_MENNYISEG"], errors="coerce").fillna(0).sum()
        company_count = base["CEGNEV"].nunique() if "CEGNEV" in base.columns else 0
        waste_count = base["HULLADEKKOD"].nunique() if "HULLADEKKOD" in base.columns else 0

        if "HULLADEK_VESZELYES_10" in base.columns:
            dangerous_qty = base.loc[
                pd.to_numeric(base["HULLADEK_VESZELYES_10"], errors="coerce").fillna(0) == 1,
                "OSSZES_MENNYISEG"
            ]
            dangerous_qty = pd.to_numeric(dangerous_qty, errors="coerce").fillna(0).sum()
        else:
            dangerous_qty = 0

        dangerous_ratio = (dangerous_qty / total_qty * 100) if total_qty else 0
        years = sorted(base["EV"].dropna().astype(int).unique()) if "EV" in base.columns else []
        year_text = f"{years[0]} - {years[-1]}" if years else "-"

        self.kpi_total.set_value(format_quantity_auto_unit(total_qty, 2))
        self.kpi_companies.set_value(str(company_count))
        self.kpi_codes.set_value(str(waste_count))
        self.kpi_danger.set_value(f"{format_hu_number(dangerous_ratio, 2)} %")
        self.kpi_years.set_value(year_text)

    # -------------------------------------------------------------------------
    # Chart helpers
    # -------------------------------------------------------------------------
    def style_axes(self, ax, title):
        ax.clear()
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.18)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    def draw_empty(self, canvas, title):
        ax = canvas.ax
        ax.clear()
        canvas.reset_hover()
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.text(0.5, 0.5, "Nincs megjeleníthető adat", ha="center", va="center", transform=ax.transAxes)
        canvas.figure.subplots_adjust(left=0.08, right=0.98, top=0.90, bottom=0.12)
        canvas.draw_idle()

    # -------------------------------------------------------------------------
    # Charts
    # -------------------------------------------------------------------------
    def render_yearly_chart(self, canvas):
        base = self.get_filtered_base_unique()
        if base.empty or "EV" not in base.columns or "OSSZES_MENNYISEG" not in base.columns:
            self.draw_empty(canvas, "Éves trend")
            return

        ax = canvas.ax
        self.style_axes(ax, "Éves trend")
        canvas.reset_hover()

        tmp = base.copy()
        tmp["OSSZES_MENNYISEG"] = pd.to_numeric(tmp["OSSZES_MENNYISEG"], errors="coerce").fillna(0)

        yearly = tmp.groupby("EV", as_index=False)["OSSZES_MENNYISEG"].sum().sort_values("EV")
        if yearly.empty:
            self.draw_empty(canvas, "Éves trend")
            return

        xs = yearly["EV"].tolist()
        ys_raw = yearly["OSSZES_MENNYISEG"].tolist()
        ys, unit = convert_for_axis(ys_raw)

        ax.plot(xs, ys, marker="o", linewidth=2.4, color="#2563eb")
        ax.fill_between(xs, ys, alpha=0.10, color="#60a5fa")
        ax.set_xlabel("Év")
        ax.set_ylabel(f"Mennyiség ({unit})")
        ax.xaxis.set_major_formatter(axis_int_formatter())
        ax.yaxis.set_major_formatter(axis_number_formatter(0))

        canvas.add_point_targets(
            xs,
            ys,
            [f"Év: {int(x)}\nMennyiség: {format_quantity_auto_unit(y, 2)}" for x, y in zip(xs, ys_raw)],
        )

        canvas.figure.subplots_adjust(left=0.10, right=0.98, top=0.90, bottom=0.16)
        canvas.draw_idle()

    def render_danger_chart(self, canvas):
        base = self.get_filtered_base_unique()
        if base.empty or "OSSZES_MENNYISEG" not in base.columns or "HULLADEK_VESZELYES_10" not in base.columns:
            self.draw_empty(canvas, "Veszélyességi megoszlás")
            return

        ax = canvas.ax
        ax.clear()
        canvas.reset_hover()

        qty = pd.to_numeric(base["OSSZES_MENNYISEG"], errors="coerce").fillna(0)
        danger = pd.to_numeric(base["HULLADEK_VESZELYES_10"], errors="coerce").fillna(0)

        dangerous_qty = qty[danger == 1].sum()
        non_dangerous_qty = qty[danger != 1].sum()

        vals = [dangerous_qty, non_dangerous_qty]
        labels = ["Veszélyes", "Nem veszélyes"]
        total = sum(vals)
        percents = [(v / total * 100) if total else 0 for v in vals]

        wedges, _ = ax.pie(
            vals,
            labels=labels,
            startangle=90,
            colors=["#ef4444", "#94a3b8"],
            wedgeprops=dict(width=0.45, edgecolor="white"),
        )
        ax.set_title("Veszélyességi megoszlás", fontsize=11, fontweight="bold")

        canvas.add_pie_targets(wedges, labels, vals, percents=percents)
        canvas.draw_idle()

    def render_finance_chart(self, canvas):
        if self.source_df.empty or "CEGNEV" not in self.source_df.columns:
            self.draw_empty(canvas, "Árbevétel / saját tőke")
            return

        src = self.source_df.copy()
        if "SRC_NETTO_ARBEV" not in src.columns:
            src["SRC_NETTO_ARBEV"] = ""
        if "SRC_SAJAT_TOKE" not in src.columns:
            src["SRC_SAJAT_TOKE"] = ""

        src["ARBEV_NUM"] = pd.to_numeric(parse_hu_numeric(src["SRC_NETTO_ARBEV"]), errors="coerce")
        src["SAJAT_TOKE_NUM"] = pd.to_numeric(parse_hu_numeric(src["SRC_SAJAT_TOKE"]), errors="coerce")

        plot_df = (
            src[["CEGNEV", "ARBEV_NUM", "SAJAT_TOKE_NUM"]]
            .drop_duplicates("CEGNEV")
            .dropna(subset=["ARBEV_NUM", "SAJAT_TOKE_NUM"], how="all")
            .copy()
        )

        if plot_df.empty:
            self.draw_empty(canvas, "Árbevétel / saját tőke")
            return

        plot_df["ARBEV_NUM"] = pd.to_numeric(plot_df["ARBEV_NUM"], errors="coerce")
        plot_df["SAJAT_TOKE_NUM"] = pd.to_numeric(plot_df["SAJAT_TOKE_NUM"], errors="coerce")
        plot_df = plot_df.fillna({"ARBEV_NUM": 0, "SAJAT_TOKE_NUM": 0})
        plot_df = plot_df.sort_values("ARBEV_NUM", ascending=False).head(8).reset_index(drop=True)

        ax = canvas.ax
        self.style_axes(ax, "Árbevétel / saját tőke")
        canvas.reset_hover()

        x = range(len(plot_df))
        width = 0.38

        arbev_m = (plot_df["ARBEV_NUM"] / 1_000_000).tolist()
        toke_m = (plot_df["SAJAT_TOKE_NUM"] / 1_000_000).tolist()
        labels = [self.shorten_label(v, 12) for v in plot_df["CEGNEV"].astype(str).tolist()]
        full_labels = plot_df["CEGNEV"].astype(str).tolist()

        bars1 = ax.bar([i - width / 2 for i in x], arbev_m, width=width, label="Árbevétel", color="#2563eb", alpha=0.95)
        bars2 = ax.bar([i + width / 2 for i in x], toke_m, width=width, label="Saját tőke", color="#8b5cf6", alpha=0.95)

        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=18, ha="right")
        ax.set_ylabel("Millió Ft")
        ax.yaxis.set_major_formatter(axis_number_formatter(0))
        ax.legend(frameon=False, fontsize=8)

        hover_texts_arbev = [
            f"{cég}\nMutató: Nettó árbevétel\nÉrték: {format_hu_number(a, 0)} Ft"
            for cég, a in zip(full_labels, plot_df["ARBEV_NUM"].tolist())
        ]
        hover_texts_toke = [
            f"{cég}\nMutató: Saját tőke\nÉrték: {format_hu_number(t, 0)} Ft"
            for cég, t in zip(full_labels, plot_df["SAJAT_TOKE_NUM"].tolist())
        ]

        canvas.add_bar_targets(bars1, hover_texts_arbev, plot_df["ARBEV_NUM"].tolist(), horizontal=False)
        canvas.add_bar_targets(bars2, hover_texts_toke, plot_df["SAJAT_TOKE_NUM"].tolist(), horizontal=False)

        canvas.figure.subplots_adjust(left=0.10, right=0.98, top=0.88, bottom=0.22)
        canvas.draw_idle()

    def render_category_chart(self, canvas):
        master = self.get_filtered_master()
        if master is None or master.empty or "PRODUCT_GROUP" not in master.columns or "OSSZES_MENNYISEG" not in master.columns:
            self.draw_empty(canvas, "Termékcsoportok megoszlása")
            return

        ax = canvas.ax
        self.style_axes(ax, "Termékcsoportok megoszlása")
        canvas.reset_hover()

        tmp = master.copy()
        tmp["OSSZES_MENNYISEG"] = pd.to_numeric(tmp["OSSZES_MENNYISEG"], errors="coerce").fillna(0)

        cat = (
            tmp.groupby("PRODUCT_GROUP", as_index=False)["OSSZES_MENNYISEG"]
            .sum()
            .sort_values("OSSZES_MENNYISEG", ascending=False)
            .head(8)
            .sort_values("OSSZES_MENNYISEG", ascending=True)
        )

        if cat.empty:
            self.draw_empty(canvas, "Termékcsoportok megoszlása")
            return

        total = cat["OSSZES_MENNYISEG"].sum()
        cat["PCT"] = (cat["OSSZES_MENNYISEG"] / total * 100) if total else 0

        full_labels = cat["PRODUCT_GROUP"].astype(str).tolist()
        short_labels = [self.shorten_label(v, 18) for v in full_labels]
        vals_raw = cat["OSSZES_MENNYISEG"].tolist()
        vals, unit = convert_for_axis(vals_raw)
        percents = cat["PCT"].tolist()

        colors = ["#2563eb", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#84cc16", "#f97316"]
        bars = ax.barh(short_labels, vals, color=colors[:len(short_labels)], alpha=0.92)

        ax.set_xlabel(f"Mennyiség ({unit})")
        ax.set_ylabel("Termékcsoport")
        ax.xaxis.set_major_formatter(axis_number_formatter(0))

        for bar, pct in zip(bars, percents):
            ax.text(
                bar.get_width(),
                bar.get_y() + bar.get_height() / 2,
                f"  {format_hu_number(pct, 1)}%",
                va="center",
                ha="left",
                fontsize=8,
            )

        hover_texts = [f"Kategória: {label}" for label in full_labels]
        canvas.add_bar_targets(bars, hover_texts, vals_raw, horizontal=True, percents=percents)

        canvas.figure.subplots_adjust(left=0.25, right=0.97, top=0.90, bottom=0.12)
        canvas.draw_idle()

    def render_company_chart(self, canvas):
        master = self.get_filtered_master()
        base = self.get_filtered_base_unique()

        if (
            master is None or master.empty or base.empty
            or "CEGNEV" not in base.columns or "OSSZES_MENNYISEG" not in base.columns
            or "PRODUCT_GROUP" not in master.columns
        ):
            self.draw_empty(canvas, "Top cégek összetétele")
            return

        base_tmp = base.copy()
        base_tmp["OSSZES_MENNYISEG"] = pd.to_numeric(base_tmp["OSSZES_MENNYISEG"], errors="coerce").fillna(0)

        top_companies = (
            base_tmp.groupby("CEGNEV", as_index=False)["OSSZES_MENNYISEG"]
            .sum()
            .sort_values("OSSZES_MENNYISEG", ascending=False)
            .head(6)["CEGNEV"].tolist()
        )

        tmp = master.copy()
        tmp["OSSZES_MENNYISEG"] = pd.to_numeric(tmp["OSSZES_MENNYISEG"], errors="coerce").fillna(0)

        sub = (
            tmp[tmp["CEGNEV"].isin(top_companies)]
            .groupby(["CEGNEV", "PRODUCT_GROUP"], as_index=False)["OSSZES_MENNYISEG"]
            .sum()
        )

        pivot = sub.pivot(index="CEGNEV", columns="PRODUCT_GROUP", values="OSSZES_MENNYISEG").fillna(0)
        if pivot.empty:
            self.draw_empty(canvas, "Top cégek összetétele")
            return

        total_by_company = pivot.sum(axis=1).sort_values(ascending=True)
        pivot = pivot.loc[total_by_company.index]

        ax = canvas.ax
        ax.clear()
        canvas.reset_hover()

        full_labels = pivot.index.tolist()
        short_labels = [self.shorten_label(v, 18) for v in full_labels]

        colors = ["#2563eb", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#84cc16", "#f97316", "#14b8a6", "#64748b"]
        lefts = [0.0] * len(pivot.index)

        for idx, col in enumerate(pivot.columns):
            vals_raw = pivot[col].tolist()
            vals_t = [v / 1000 for v in vals_raw]

            bars = ax.barh(
                short_labels,
                vals_t,
                left=lefts,
                label=str(col),
                color=colors[idx % len(colors)],
                alpha=0.92,
            )

            for i, bar in enumerate(bars):
                bar._x0 = lefts[i]

            hover_texts = [f"Cég: {company}\nKategória: {col}" for company in full_labels]
            canvas.add_bar_targets(bars, hover_texts, vals_raw, horizontal=True)

            lefts = [l + v for l, v in zip(lefts, vals_t)]

        ax.set_title("Top cégek összetétele", fontsize=11, fontweight="bold")
        ax.set_xlabel("Mennyiség (t)")
        ax.set_ylabel("Cég")
        ax.xaxis.set_major_formatter(axis_number_formatter(0))
        ax.legend(frameon=False, fontsize=7, loc="lower right", ncols=2)
        ax.grid(axis="x", linestyle="--", alpha=0.18)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        canvas.figure.subplots_adjust(left=0.25, right=0.97, top=0.90, bottom=0.12)
        canvas.draw_idle()

    # -------------------------------------------------------------------------
    # Table tabs
    # -------------------------------------------------------------------------
    def refresh_data_year_tabs(self):
        while self.data_year_tabs.count():
            self.data_year_tabs.removeTab(0)

        master = self.get_filtered_master()
        if master is None or master.empty:
            page = QWidget()
            l = QVBoxLayout(page)
            l.addWidget(QLabel("Nincs megjeleníthető adat."))
            self.data_year_tabs.addTab(page, "Nincs adat")
            return

        if "EV" not in master.columns:
            page = QWidget()
            l = QVBoxLayout(page)
            l.addWidget(QLabel("Az adatok nem tartalmaznak év mezőt."))
            self.data_year_tabs.addTab(page, "Nincs év")
            return

        years = sorted(master["EV"].dropna().astype(int).unique(), reverse=True)
        for year in [None] + years:
            sub = master if year is None else master[master["EV"].astype("Int64") == year]
            tab_name = "Összes" if year is None else str(year)

            out = sub.copy()

            if "HULLADEK_VESZELYES_10" in out.columns:
                out["Veszélyes"] = out["HULLADEK_VESZELYES_10"].apply(
                    lambda x: "Igen" if pd.to_numeric(x, errors="coerce") == 1 else "Nem"
                )
            else:
                out["Veszélyes"] = ""

            out = self.add_quantity_columns(out)

            preferred = [
                "CEGNEV",
                "EV",
                "ADATTIPUS_LABEL",
                "PRODUCT_GROUP",
                "HULLADEKKOD",
                "HULLADEK_MEGNEVEZES",
                "Veszélyes",
                "Mennyiség (kg)",
                "Mennyiség (t)",
            ]
            existing = [c for c in preferred if c in out.columns]
            if "Veszélyes" not in existing:
                existing.append("Veszélyes")

            out = out[existing].copy()
            out.columns = [
                "Cég" if c == "CEGNEV" else
                "Év" if c == "EV" else
                "Adattípus" if c == "ADATTIPUS_LABEL" else
                "Termékcsoport" if c == "PRODUCT_GROUP" else
                "Hulladékkód" if c == "HULLADEKKOD" else
                "Megnevezés" if c == "HULLADEK_MEGNEVEZES" else
                c
                for c in out.columns
            ]

            page = QWidget()
            page_l = QVBoxLayout(page)
            table = QTableWidget()
            self.configure_table(table)
            self._set_table_df(table, out)
            page_l.addWidget(table)
            self.data_year_tabs.addTab(page, tab_name)

    def refresh_ktj_tab(self):
        df = self.get_filtered_raw()

        if df.empty:
            self._set_table_df(self.ktj_table, pd.DataFrame())
            return

        out = df.copy()

        if "KUJ" not in out.columns:
            if "SOURCE_KUJ" in out.columns:
                out["KUJ"] = out["SOURCE_KUJ"]
            else:
                out["KUJ"] = ""

        if "KTJ" not in out.columns:
            if "SOURCE_KTJ" in out.columns:
                out["KTJ"] = out["SOURCE_KTJ"]
            else:
                out["KTJ"] = ""

        out["KUJ"] = out["KUJ"].apply(self.clean_int_like)
        out["KTJ"] = out["KTJ"].apply(self.clean_int_like)

        if "HULLADEK_VESZELYES_10" in out.columns:
            out["Veszélyes"] = out["HULLADEK_VESZELYES_10"].apply(
                lambda x: "Igen" if pd.to_numeric(x, errors="coerce") == 1 else "Nem"
            )
        else:
            out["Veszélyes"] = ""

        if "PRODUCT_GROUP" not in out.columns and "HULLADEKKOD" in out.columns:
            out["PRODUCT_GROUP"] = out["HULLADEKKOD"].astype(str).apply(self.resolve_product_group)

        if "OSSZES_MENNYISEG" in out.columns:
            out["Összes mennyiség"] = out["OSSZES_MENNYISEG"].apply(lambda x: format_quantity_auto_unit(x, 2))
        elif "MENNYISEG" in out.columns:
            out["Összes mennyiség"] = out["MENNYISEG"].apply(lambda x: format_quantity_auto_unit(x, 2))
        else:
            out["Összes mennyiség"] = ""

        self.set_status("KTJ metaadatok betöltése...")
        self.ensure_ktj_metadata_loaded(out)
        out = self.enrich_with_ktj_metadata(out)

        preferred_cols = [
            "CEGNEV",
            "KUJ",
            "KTJ",
            "KTJ megnevezés",
            "Besorolás",
            "Irányítószám",
            "Helység",
            "Cím",
            "Megye",
            "Régió",
            "EV",
            "ADATTIPUS_LABEL",
            "PRODUCT_GROUP",
            "HULLADEKKOD",
            "HULLADEK_MEGNEVEZES",
            "Veszélyes",
            "Összes mennyiség",
        ]

        existing_cols = [c for c in preferred_cols if c in out.columns]
        out = out[existing_cols].copy()

        out.rename(columns={
            "CEGNEV": "Cég",
            "KUJ": "KÜJ",
            "KTJ": "KTJ",
            "EV": "Év",
            "ADATTIPUS_LABEL": "Adattípus",
            "PRODUCT_GROUP": "Termékcsoport",
            "HULLADEKKOD": "Hulladékkód",
            "HULLADEK_MEGNEVEZES": "Megnevezés",
        }, inplace=True)

        sort_cols = [c for c in ["KÜJ", "KTJ", "Cég", "Év", "Termékcsoport", "Hulladékkód"] if c in out.columns]
        if sort_cols:
            out = out.sort_values(sort_cols, ascending=True, na_position="last")

        self._set_table_df(self.ktj_table, out)
        self.set_status("KTJ nézet frissítve.")

    def refresh_missing_table(self):
        self._set_table_df(self.missing_table, self.missing_df)

    def _set_table_df(self, table, df):
        table.setSortingEnabled(False)
        table.clear()

        if df is None or df.empty:
            table.setRowCount(0)
            table.setColumnCount(0)
            table.setSortingEnabled(True)
            return

        display_df = df.fillna("")
        table.setColumnCount(len(display_df.columns))
        table.setRowCount(len(display_df))
        table.setHorizontalHeaderLabels([str(c) for c in display_df.columns])

        for i in range(len(display_df)):
            for j, col in enumerate(display_df.columns):
                value = display_df.iloc[i, j]
                if isinstance(value, float):
                    if pd.isna(value):
                        text = ""
                    else:
                        text = str(value)
                else:
                    text = str(value)
                table.setItem(i, j, QTableWidgetItem(text))

        table.resizeColumnsToContents()
        table.setSortingEnabled(True)

    # -------------------------------------------------------------------------
    # Summary / company info
    # -------------------------------------------------------------------------
    def refresh_exec_summary(self):
        master = self.get_filtered_master()
        base = self.get_filtered_base_unique()

        if master is None or master.empty or base.empty or "OSSZES_MENNYISEG" not in base.columns:
            self.exec_text.setPlainText("Nincs megjeleníthető adat.")
            return

        qty = pd.to_numeric(base["OSSZES_MENNYISEG"], errors="coerce").fillna(0)
        total_qty = qty.sum()
        company_count = base["CEGNEV"].nunique() if "CEGNEV" in base.columns else 0
        waste_count = base["HULLADEKKOD"].nunique() if "HULLADEKKOD" in base.columns else 0
        category_count = master["PRODUCT_GROUP"].nunique() if "PRODUCT_GROUP" in master.columns else 0
        years = sorted(base["EV"].dropna().astype(int).unique()) if "EV" in base.columns else []

        if "HULLADEK_VESZELYES_10" in base.columns:
            dangerous_qty = qty[pd.to_numeric(base["HULLADEK_VESZELYES_10"], errors="coerce").fillna(0) == 1].sum()
        else:
            dangerous_qty = 0

        dangerous_ratio = (dangerous_qty / total_qty * 100) if total_qty else 0

        lines = [
            f"• Valós összes mennyiség: {format_quantity_auto_unit(total_qty, 2)}",
            f"• Cégek száma: {company_count}",
            f"• Hulladékkódok száma: {waste_count}",
            f"• Termékcsoportok száma: {category_count}",
            f"• Veszélyességi arány: {format_hu_number(dangerous_ratio, 2)} %",
            f"• Vizsgált évek: {years[0]} - {years[-1]}" if years else "• Vizsgált évek: nincs adat",
            "• A termékcsoport összesítések átfedhetnek, mert egy kód több kategóriába is tartozhat, a nem listázott kódok 'Nem besorolható' kategóriába kerülnek.",
        ]
        self.exec_text.setPlainText("\n".join(lines))

    def refresh_company_info(self):
        selected = self.filter_company.checked_texts()
        if len(selected) != 1:
            self.company_info.setPlainText("Válassz ki pontosan 1 céget.")
            return

        company = selected[0]
        if self.source_df.empty or "CEGNEV" not in self.source_df.columns:
            self.company_info.setPlainText("Nincs forrás adat.")
            return

        sub = self.source_df[self.source_df["CEGNEV"].astype(str) == company]
        if sub.empty:
            self.company_info.setPlainText("Nincs forrás adat.")
            return

        row = sub.iloc[0].to_dict()
        address = split_address_parts(row.get("SZEKHELY"))

        lines = [
            f"Cég: {safe_str(row.get('CEGNEV'))}",
            f"KÜJ: {safe_str(row.get('KUJ'))}",
            f"Adószám: {safe_str(row.get('ADOSZAM'))}",
            f"Vármegye: {safe_str(address.get('Vármegye'))}",
            f"IRSZ: {safe_str(address.get('IRSZ'))}",
            f"Település: {safe_str(address.get('Település'))}",
            f"Cím: {safe_str(address.get('Cím'))}",
            f"Email: {safe_str(row.get('SRC_EMAIL'))}",
            f"Telefonszám: {format_phone(row.get('SRC_TELEFON'))}",
            f"Web: {safe_str(row.get('SRC_WEB'))}",
            f"Saját tőke: {safe_str(row.get('SRC_SAJAT_TOKE'))}",
            f"Nettó árbevétel: {safe_str(row.get('SRC_NETTO_ARBEV'))}",
        ]
        self.company_info.setPlainText("\n".join(lines))

    # -------------------------------------------------------------------------
    # Dialog / export / persistence / cache
    # -------------------------------------------------------------------------
    def open_chart(self, title, renderer):
        FullscreenChartDialog(title, renderer, self).exec()

    def save_snapshot(self):
        if self.source_df.empty and self.raw_df.empty:
            QMessageBox.warning(self, "Nincs adat", "Nincs mit menteni.")
            return

        note, ok = QInputDialog.getText(self, "Mentés", "Megjegyzés:")
        if not ok:
            return

        try:
            snapshot_id = self.persistence.save_snapshot(
                self.source_df,
                self.raw_df,
                self.missing_df,
                source_file=self.current_file_path.strip(),
                note=note,
            )
            self.set_status(f"Snapshot mentve: {snapshot_id}")
            QMessageBox.information(self, "Siker", f"Mentés kész. Snapshot ID: {snapshot_id}")
        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def load_snapshot(self):
        snapshots = self.persistence.list_snapshots()
        if not snapshots:
            QMessageBox.information(self, "Nincs mentés", "Nincsenek mentett snapshotok.")
            return

        items = [f"{sid} | {created} | {src or '-'} | {note or '-'}" for sid, created, src, note in snapshots]
        selected, ok = QInputDialog.getItem(self, "Betöltés", "Válassz snapshotot:", items, 0, False)
        if not ok or not selected:
            return

        snapshot_id = int(selected.split("|")[0].strip())

        try:
            self.set_status(f"Snapshot betöltése: {snapshot_id}")
            self.source_df, self.raw_df, self.missing_df = self.persistence.load_snapshot(snapshot_id)
            self.agg = aggregate_data(self.raw_df, self.source_df, self.category_service)
            self.populate_filters()
            self.apply_default_year_selection()
            self.refresh_all_views()
            self.set_status(f"Snapshot betöltve: {snapshot_id}")
            QMessageBox.information(self, "Siker", f"Snapshot betöltve: {snapshot_id}")
        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def export_shared_package(self):
        if self.source_df.empty and self.raw_df.empty and self.missing_df.empty:
            QMessageBox.warning(self, "Nincs adat", "Nincs exportálható munkamenet.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Megosztott csomag mentése",
            str(OUTPUT_DIR / "okir_megoszthato_csomag.okirpkg"),
            "OKIR Package (*.okirpkg);;ZIP Files (*.zip)",
        )
        if not path:
            return

        try:
            self.set_status("Megosztott csomag készítése...")
            state = self.capture_state()
            self.persistence.export_shared_package(
                package_path=path,
                source_df=self.source_df,
                raw_df=self.raw_df,
                missing_df=self.missing_df,
                state=state,
                ktj_meta_cache=self.ktj_meta_cache,
            )
            self.set_status(f"Megosztott csomag elkészült: {path}")
            QMessageBox.information(self, "Siker", f"Megosztott csomag elkészült:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def import_shared_package(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Megosztott csomag betöltése",
            str(OUTPUT_DIR),
            "OKIR Package (*.okirpkg);;ZIP Files (*.zip)",
        )
        if not path:
            return

        try:
            self.set_status("Megosztott csomag betöltése...")
            source_df, raw_df, missing_df, state, ktj_meta = self.persistence.import_shared_package(path)

            self.source_df = source_df if source_df is not None else pd.DataFrame()
            self.raw_df = raw_df if raw_df is not None else pd.DataFrame()
            self.missing_df = missing_df if missing_df is not None else pd.DataFrame()
            self.ktj_meta_cache = ktj_meta if isinstance(ktj_meta, dict) else {}

            self.agg = aggregate_data(self.raw_df, self.source_df, self.category_service)
            self.restore_state(state)

            self.set_status(f"Megosztott csomag betöltve: {path}")
            QMessageBox.information(self, "Siker", f"Megosztott csomag betöltve:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def export_summary_excel(self):
        master = self.get_filtered_master()
        if master is None or master.empty:
            QMessageBox.warning(self, "Nincs adat", "Nincs exportálható adat.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Összesítő export mentése",
            str(OUTPUT_DIR / "okir_summary_export.xlsx"),
            "Excel Files (*.xlsx)",
        )
        if not path:
            return

        try:
            self.set_status("Összesítő export készítése...")
            groups = [g for g in self.category_service.all_groups() if g != "Nem besorolható"] + ["Nem besorolható"]
            out = build_summary_export(master, self.source_df, groups)

            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                out.to_excel(writer, index=False, sheet_name="Összesítő")
                style_excel_worksheet(writer.book["Összesítő"])

            self.set_status(f"Összesítő export elkészült: {path}")
            QMessageBox.information(self, "Siker", f"Export elkészült:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def export_detail_excel(self):
        master = self.get_filtered_master()
        if master is None or master.empty:
            QMessageBox.warning(self, "Nincs adat", "Nincs exportálható adat.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Részletes export mentése",
            str(OUTPUT_DIR / "okir_detail_export.xlsx"),
            "Excel Files (*.xlsx)",
        )
        if not path:
            return

        try:
            self.set_status("Részletes export készítése...")
            out = build_detail_export(master, self.source_df)
            pivot = build_detail_pivot_export(master)

            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                out.to_excel(writer, index=False, sheet_name="Részletes")
                style_excel_worksheet(writer.book["Részletes"])
                ws = writer.book.create_sheet("Kimutatás")
                build_outline_report_sheet(ws, pivot)

            self.set_status(f"Részletes export elkészült: {path}")
            QMessageBox.information(self, "Siker", f"Export elkészült:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def export_ktj_excel(self):
        df = self.get_filtered_raw()
        if df.empty:
            QMessageBox.warning(self, "Nincs adat", "Nincs exportálható adat.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "KTJ szerinti export mentése",
            str(OUTPUT_DIR / "okir_ktj_export.xlsx"),
            "Excel Files (*.xlsx)",
        )
        if not path:
            return

        try:
            self.set_status("KTJ szerinti export készítése...")

            out = df.copy()

            if "KUJ" not in out.columns:
                if "SOURCE_KUJ" in out.columns:
                    out["KUJ"] = out["SOURCE_KUJ"]
                else:
                    out["KUJ"] = ""

            if "KTJ" not in out.columns:
                if "SOURCE_KTJ" in out.columns:
                    out["KTJ"] = out["SOURCE_KTJ"]
                else:
                    out["KTJ"] = ""

            out["KUJ"] = out["KUJ"].apply(self.clean_int_like)
            out["KTJ"] = out["KTJ"].apply(self.clean_int_like)

            if "HULLADEK_VESZELYES_10" in out.columns:
                out["Veszélyes"] = out["HULLADEK_VESZELYES_10"].apply(
                    lambda x: "Igen" if pd.to_numeric(x, errors="coerce") == 1 else "Nem"
                )
            else:
                out["Veszélyes"] = ""

            if "PRODUCT_GROUP" not in out.columns and "HULLADEKKOD" in out.columns:
                out["PRODUCT_GROUP"] = out["HULLADEKKOD"].astype(str).apply(self.resolve_product_group)

            self.ensure_ktj_metadata_loaded(out)
            out = self.enrich_with_ktj_metadata(out)

            if "OSSZES_MENNYISEG" in out.columns:
                qty_col = "OSSZES_MENNYISEG"
            elif "MENNYISEG" in out.columns:
                qty_col = "MENNYISEG"
            else:
                qty_col = None

            export_cols = [
                "CEGNEV",
                "KUJ",
                "KTJ",
                "KTJ megnevezés",
                "Besorolás",
                "Irányítószám",
                "Helység",
                "Cím",
                "Megye",
                "Régió",
                "EV",
                "ADATTIPUS_LABEL",
                "PRODUCT_GROUP",
                "HULLADEKKOD",
                "HULLADEK_MEGNEVEZES",
                "Veszélyes",
            ]
            if qty_col:
                export_cols.append(qty_col)

            export_cols = [c for c in export_cols if c in out.columns]
            out = out[export_cols].copy()

            rename_map = {
                "CEGNEV": "Cég",
                "KUJ": "KÜJ",
                "KTJ": "KTJ",
                "EV": "Év",
                "ADATTIPUS_LABEL": "Adattípus",
                "PRODUCT_GROUP": "Termékcsoport",
                "HULLADEKKOD": "Hulladékkód",
                "HULLADEK_MEGNEVEZES": "Megnevezés",
            }
            if qty_col:
                rename_map[qty_col] = "Összes mennyiség"

            out.rename(columns=rename_map, inplace=True)

            sort_cols = [c for c in ["KÜJ", "KTJ", "Cég", "Év", "Termékcsoport", "Hulladékkód"] if c in out.columns]
            if sort_cols:
                out.sort_values(sort_cols, inplace=True, na_position="last")

            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                out.to_excel(writer, index=False, sheet_name="KTJ szerint")
                style_excel_worksheet(writer.book["KTJ szerint"])

            self.set_status(f"KTJ export elkészült: {path}")
            QMessageBox.information(self, "Siker", f"Export elkészült:\n{path}")

        except Exception as e:
            QMessageBox.critical(self, "Hiba", str(e))

    def clear_cache(self):
        reply = QMessageBox.question(self, "Megerősítés", "Biztos törlöd a cache teljes tartalmát?")
        if reply == QMessageBox.Yes:
            self.cache.clear_all()
            self.set_status("A gyorsítótár törölve lett.")
            QMessageBox.information(self, "Kész", "A cache törölve lett.")