import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

import pandas as pd
from PySide6.QtCore import QThread, Signal

from core.constants import ADATTIPUS_LABELS, CACHE_DB, DEFAULT_MAX_WORKERS
from services.aggregation_service import extract_records, normalize_records
from services.cache_db import CacheDB
from services.okir_client import OkirClient


class FetchWorker(QThread):
    progress_signal = Signal(int, int, str)
    finished_signal = Signal(object, object, object, object)
    error_signal = Signal(str)

    def __init__(self, source_df, use_cache, force_refresh, selected_types, max_workers: int = DEFAULT_MAX_WORKERS):
        super().__init__()
        self.source_df = source_df
        self.use_cache = use_cache
        self.force_refresh = force_refresh
        self.selected_types = selected_types
        self.max_workers = min(6, max(1, int(max_workers or DEFAULT_MAX_WORKERS)))

    def run(self):
        try:
            tasks: List[Tuple[str, str, str]] = []
            for _, row in self.source_df.iterrows():
                company = str(row.get("CEGNEV", "")).strip()
                kuj = str(row.get("KUJ", "")).strip()
                for adattipus in self.selected_types:
                    tasks.append((company, kuj, adattipus))

            total = max(len(tasks), 1)
            step = 0
            all_rows: List[Dict] = []
            missing: List[Dict] = []
            years: List[int] = []
            local = threading.local()

            def job(company: str, kuj: str, adattipus: str):
                label = ADATTIPUS_LABELS.get(adattipus, adattipus)

                if not hasattr(local, "cache"):
                    local.cache = CacheDB(CACHE_DB)
                if not hasattr(local, "client"):
                    local.client = OkirClient()

                cache = local.cache
                client = local.client
                data = None

                if self.use_cache and not self.force_refresh:
                    cached = cache.get(kuj, adattipus)
                    if cached and cached["status"] == "success" and cached["raw_json"]:
                        try:
                            data = json.loads(cached["raw_json"])
                        except Exception:
                            data = None

                if data is None:
                    try:
                        data = client.fetch(kuj, adattipus)
                        cache.save(kuj, adattipus, "success", json.dumps(data, ensure_ascii=False), "", "requests")
                    except Exception as e:
                        err = str(e)
                        cache.save(kuj, adattipus, "error", "", err, "requests")
                        return company, kuj, label, None, err

                records = extract_records(data, adattipus)
                if not records:
                    return company, kuj, label, None, "Nincs feldolgozható rekord"

                rows = normalize_records(records, company, kuj, adattipus)
                return company, kuj, label, rows, None

            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = [ex.submit(job, c, k, a) for c, k, a in tasks]

                for fut in as_completed(futures):
                    step += 1
                    try:
                        company, kuj, label, rows, err = fut.result()
                    except Exception as e:
                        company, kuj, label, rows, err = "-", "-", "-", None, str(e)

                    self.progress_signal.emit(step, total, f"Lekérés: {company} / {label}")

                    if err:
                        missing.append({
                            "Cég": company,
                            "KÜJ": kuj,
                            "Adattípus": label,
                            "Ok": err,
                        })
                        continue

                    for r in rows or []:
                        y = pd.to_numeric(r.get("EV"), errors="coerce")
                        if pd.notna(y):
                            years.append(int(y))
                    all_rows.extend(rows or [])

            self.finished_signal.emit(
                self.source_df,
                pd.DataFrame(all_rows),
                pd.DataFrame(missing),
                max(years) if years else None,
            )
        except Exception as e:
            self.error_signal.emit(str(e))