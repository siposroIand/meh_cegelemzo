from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.utils import adoszam_to_kshkod, normalize_header, parse_date_to_str, safe_str


HEADER_ALIASES = {
    "Teljes név": ["teljes név", "teljes nev", "cégnév", "cegnev"],
    "Adószám": ["adószám", "adoszam"],
    "Cégforma": ["cégforma", "cegforma"],
    "Megye": ["megye"],
    "Irányítószám": ["irányítószám", "iranyitoszam"],
    "Település": ["település", "telepules"],
    "Cím": ["cím", "cim"],
    "Alapítás dátuma": ["alapítás dátuma", "alapitas datuma"],
    "Utolsó létszám": ["utolsó létszám", "utolso letszam"],
    "Fő tevékenység": ["fő tevékenység", "fo tevekenyseg"],
    "Vezetők száma": ["vezetők száma", "vezetok szama"],
    "Tisztségviselők": ["tisztségviselők", "tiszsegviselok"],
    "Telephelyek száma": ["telephelyek száma", "telephelyek szama"],
    " Saját tőke ": ["saját tőke", "sajat toke"],
    " Nettó árbevétel ": ["nettó árbevétel", "netto arbevetel"],
    "Email": ["email", "e-mail"],
    "Telefonszám": ["telefonszám", "telefonszam", "telefon"],
    "Web cím": ["web cím", "web cim", "web", "honlap"],
}

OPTIONAL_MAP = {
    "Cégforma": ("SRC_CEGFORMA", "str"),
    "Alapítás dátuma": ("SRC_ALAPITAS_DATUMA", "date"),
    "Utolsó létszám": ("SRC_UT_LETSZAM", "str"),
    "Fő tevékenység": ("SRC_FO_TEV", "str"),
    "Vezetők száma": ("SRC_VEZETOK_SZAMA", "str"),
    "Tisztségviselők": ("SRC_TISZTSEGVISELOK", "str"),
    "Telephelyek száma": ("SRC_TELEPHELYEK_SZAMA", "str"),
    " Saját tőke ": ("SRC_SAJAT_TOKE", "str"),
    " Nettó árbevétel ": ("SRC_NETTO_ARBEV", "str"),
    "Email": ("SRC_EMAIL", "str"),
    "Telefonszám": ("SRC_TELEFON", "str"),
    "Web cím": ("SRC_WEB", "str"),
}


def _build_header_map(df: pd.DataFrame) -> Dict[str, str]:
    normalized = {normalize_header(c): c for c in df.columns}
    out = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key = normalize_header(alias)
            if key in normalized:
                out[canonical] = normalized[key]
                break
    return out


def build_source_from_excel_by_adoszam(
    excel_path: str,
    client,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[int, int, str], None]] = None,
) -> pd.DataFrame:
    df = pd.read_excel(excel_path)
    header_map = _build_header_map(df)

    required = ["Teljes név", "Adószám", "Megye", "Irányítószám", "Település", "Cím"]
    missing = [col for col in required if col not in header_map]
    if missing:
        raise Exception("Hiányzó kötelező oszlopok: " + ", ".join(missing))

    records: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    total = len(df)

    for idx, row in df.iterrows():
        company_name_from_excel = safe_str(row.get(header_map["Teljes név"]))
        raw_adoszam = row.get(header_map["Adószám"])

        if progress_fn and (idx % 5 == 0 or idx == total - 1):
            progress_fn(idx + 1, max(total, 1), f"Forrás beolvasás: {company_name_from_excel or '-'}")

        szekhely = ", ".join([
            p for p in [
                safe_str(row.get(header_map.get("Megye", ""))),
                safe_str(row.get(header_map.get("Irányítószám", ""))),
                safe_str(row.get(header_map.get("Település", ""))),
                safe_str(row.get(header_map.get("Cím", ""))),
            ] if p
        ])

        kshkod = adoszam_to_kshkod(raw_adoszam)

        if log_fn:
            log_fn(f"{idx + 1}. sor | {company_name_from_excel or '-'} | Adószám: {raw_adoszam} | KSHKOD: {kshkod or 'érvénytelen'}")

        if not kshkod:
            continue

        src_extras: Dict[str, Any] = {}
        for canonical, (target, kind) in OPTIONAL_MAP.items():
            source_col = header_map.get(canonical)
            if source_col:
                val = row.get(source_col)
                src_extras[target] = parse_date_to_str(val) if kind == "date" else safe_str(val)

        try:
            companies = client.fetch_companies_by_kshkod(kshkod)
        except Exception as e:
            if log_fn:
                log_fn(f"KSHKOD lekérési hiba: {kshkod} | {e}")
            continue

        for item in companies:
            cegnev = safe_str(item.get("MEGNEVEZES"))
            kuj = safe_str(item.get("KUJ"))
            found_ksh = safe_str(item.get("KSHKOD"))

            if not cegnev or not kuj:
                continue

            key = (cegnev, kuj)
            if key in seen:
                continue
            seen.add(key)

            record = {
                "CEGNEV": cegnev,
                "CEGNEV_FORRAS": company_name_from_excel,
                "KUJ": kuj,
                "KSHKOD": found_ksh if found_ksh else kshkod,
                "ADOSZAM": safe_str(raw_adoszam),
                "SZEKHELY": szekhely,
            }
            record.update(src_extras)
            records.append(record)

    result = pd.DataFrame(records)
    if not result.empty:
        result = result.drop_duplicates(subset=["CEGNEV", "KUJ"]).reset_index(drop=True)
    return result