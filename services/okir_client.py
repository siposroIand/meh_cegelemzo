import random
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from core.constants import ENDPOINTS, KSH_SEARCH_URL, REQUEST_JITTER_MAX, REQUEST_JITTER_MIN
from core.utils import try_json_load


KTJ_SEARCH_URL = "https://web.okir.hu/licoms/dbb-mybatis/KARLOW02/KTJ/{ktj}/ORDER_BY/KTJ/DIR/ASC/"


class RequestLimiter:
    def __init__(self, min_delay=0.18, max_delay=0.65):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            delay = random.uniform(self.min_delay, self.max_delay)
            delta = now - self._last_ts
            if delta < delay:
                time.sleep(delay - delta)
            self._last_ts = time.time()


_SHARED_LIMITER = RequestLimiter(REQUEST_JITTER_MIN, REQUEST_JITTER_MAX)


class OkirClient:
    def __init__(self, limiter: Optional[RequestLimiter] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "hu,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://web.okir.hu",
            "Referer": "https://web.okir.hu/sse/",
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.limiter = limiter or _SHARED_LIMITER

    def fetch_companies_by_kshkod(self, kshkod: str, timeout: int = 30) -> List[Dict[str, Any]]:
        self.limiter.wait()
        url = KSH_SEARCH_URL.format(kshkod=kshkod)
        r = self.session.post(url, data={"start": "0", "limit": "100"}, timeout=(10, timeout))
        r.raise_for_status()
        return r.json().get("myData", [])

    def fetch(self, kuj: str, adattipus: str, retries: int = 4, timeout: int = 30) -> Any:
        url = ENDPOINTS[adattipus].format(kuj=kuj)
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                self.limiter.wait()
                r = self.session.get(url, timeout=(10, timeout))
                r.raise_for_status()
                text = (r.text or "").strip()
                if not text:
                    raise Exception("Üres válasz")
                try:
                    return r.json()
                except Exception:
                    loaded = try_json_load(text)
                    if loaded is None:
                        raise Exception("Nem JSON válasz")
                    return loaded
            except Exception as e:
                last_error = e
                time.sleep(min(8.0, attempt * 1.15 + random.random() * 0.35))

        raise Exception(str(last_error) if last_error else "Sikertelen lekérés")

    def fetch_ktj_details(self, ktj: str, retries: int = 4, timeout: int = 30) -> List[Dict[str, Any]]:
        url = KTJ_SEARCH_URL.format(ktj=str(ktj).strip())
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                self.limiter.wait()
                r = self.session.post(
                    url,
                    data={"start": "0", "limit": "100"},
                    timeout=(10, timeout),
                )
                r.raise_for_status()

                text = (r.text or "").strip()
                if not text:
                    raise Exception("Üres válasz")

                try:
                    payload = r.json()
                except Exception:
                    payload = try_json_load(text)
                    if payload is None:
                        raise Exception("Nem JSON válasz")

                if isinstance(payload, dict):
                    data = payload.get("myData", [])
                    if isinstance(data, list):
                        return data
                    return []

                raise Exception("Váratlan KTJ válaszformátum")

            except Exception as e:
                last_error = e
                time.sleep(min(8.0, attempt * 1.15 + random.random() * 0.35))

        raise Exception(str(last_error) if last_error else "Sikertelen KTJ lekérés")

    def fetch_first_ktj_detail(self, ktj: str, retries: int = 4, timeout: int = 30) -> Optional[Dict[str, Any]]:
        items = self.fetch_ktj_details(ktj=ktj, retries=retries, timeout=timeout)
        return items[0] if items else None