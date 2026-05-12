from pathlib import Path

APP_DIR = Path.cwd()
OUTPUT_DIR = APP_DIR / "output"
LOG_DIR = APP_DIR / "logs"
DATA_DIR = APP_DIR / "data"
CONFIG_DIR = APP_DIR / "config"

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)

CACHE_DB = DATA_DIR / "okir_cache.db"
PRODUCT_GROUPS_JSON = CONFIG_DIR / "product_groups.json"

KSH_SEARCH_URL = "https://web.okir.hu/licoms/dbb-mybatis/KARLOW01/KSHKOD/{kshkod}/ORDER_BY/KUJ/DIR/ASC/"

ENDPOINTS = {
    "HULL_KELETKEZES": "https://web.okir.hu/licoms/dbb-popup/EHIRLOW19/KUJ/{kuj}/QU/HULL_KELETKEZES/",
    "HULL_ATVETEL": "https://web.okir.hu/licoms/dbb-popup/EHIRLOW21/KUJ/{kuj}/QU/HULL_ATVETEL/",
    "HULL_ATADAS": "https://web.okir.hu/licoms/dbb-popup/EHIRLOW20/KUJ/{kuj}/QU/HULL_ATADAS/",
}

ADATTIPUS_LABELS = {
    "HULL_KELETKEZES": "Keletkezés",
    "HULL_ATVETEL": "Átvétel",
    "HULL_ATADAS": "Átadás",
}
LABEL_TO_ADATTIPUS = {v: k for k, v in ADATTIPUS_LABELS.items()}

DEFAULT_QUERY_TYPE_LABEL = "Átadás"

DEFAULT_MAX_WORKERS = 4
REQUEST_JITTER_MIN = 0.18
REQUEST_JITTER_MAX = 0.65
FILTER_DEBOUNCE_MS = 220