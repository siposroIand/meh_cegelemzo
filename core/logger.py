import logging
from datetime import datetime
from typing import List

from core.constants import LOG_DIR


class Logger:
    def __init__(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"okir_{ts}.log"
        self.lines: List[str] = []
        self._logger = logging.getLogger(f"okir.logger.{ts}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        handler = logging.FileHandler(self.path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.handlers.clear()
        self._logger.addHandler(handler)

    def write(self, msg: str):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        self.lines.append(line)
        print(line)
        self._logger.info(line)

    def text(self) -> str:
        return "\n".join(self.lines)


logger = Logger()
