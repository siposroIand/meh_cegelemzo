from datetime import datetime
from typing import List
from core.constants import LOG_DIR


class Logger:
    def __init__(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"okir_{ts}.log"
        self.lines: List[str] = []

    def write(self, msg: str):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        self.lines.append(line)
        print(line)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def text(self) -> str:
        return "\n".join(self.lines)


logger = Logger()