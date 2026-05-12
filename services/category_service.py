import json
from pathlib import Path
from typing import Dict, List


DEFAULT_GROUPS = {
    "Vas": [],
    "Alu": [],
    "Réz": [],
    "Egyéb fémek": [],
    "Papír": [],
    "Műanyag": [],
    "Elektronika": [],
    "Fa": [],
    "Bontási építési": [],
    "Nem besorolható": []
}


class CategoryService:
    def __init__(self, json_path: Path):
        self.json_path = json_path
        self.groups = self._load_or_create()
        self._normalized_groups = {
            str(group): [str(code).strip() for code in codes if str(code).strip()]
            for group, codes in self.groups.items()
        }

    def _load_or_create(self) -> Dict[str, List[str]]:
        if not self.json_path.exists():
            self.json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_GROUPS, f, ensure_ascii=False, indent=2)
            return DEFAULT_GROUPS

        with open(self.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return DEFAULT_GROUPS

        if "Többi" in data and "Nem besorolható" not in data:
            data["Nem besorolható"] = data.pop("Többi")

        return data

    def resolve_groups(self, waste_code: str) -> List[str]:
        code = str(waste_code or "").strip()
        if not code:
            return ["Nem besorolható"]

        matches = []
        for group, codes in self._normalized_groups.items():
            if group == "Nem besorolható":
                continue
            if code in codes:
                matches.append(group)

        return matches if matches else ["Nem besorolható"]

    def all_groups(self) -> List[str]:
        return list(self.groups.keys())