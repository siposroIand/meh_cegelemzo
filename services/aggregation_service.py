from typing import Any, Dict, List

import pandas as pd

from core.constants import ADATTIPUS_LABELS
from core.utils import parse_hu_numeric
from models.records import Aggregates


def extract_records(data: Any, preferred_key: str) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if preferred_key in data and isinstance(data[preferred_key], list):
            return data[preferred_key]
        for key in ["HULL_KELETKEZES", "HULL_ATVETEL", "HULL_ATADAS", "result", "data", "rows", "items"]:
            if key in data and isinstance(data[key], list):
                return data[key]
        for _, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def normalize_records(records: List[Dict[str, Any]], company_name: str, source_kuj: str, adattipus: str) -> List[Dict[str, Any]]:
    rows = []
    for item in records:
        rows.append({
            "CEGNEV": str(company_name).strip(),
            "SOURCE_KUJ": str(source_kuj).strip(),
            "ADATTIPUS": adattipus,
            "ADATTIPUS_LABEL": ADATTIPUS_LABELS.get(adattipus, adattipus),
            "KUJ": item.get("KUJ"),
            "KTJ": item.get("KTJ"),
            "EV": pd.to_numeric(item.get("EV"), errors="coerce"),
            "HULLADEKKOD": str(item.get("HULLADEKKOD", "")).strip(),
            "HULLADEK_MEGNEVEZES": item.get("HULLADEK_MEGNEVEZES"),
            "HULLADEK_VESZELYES_10": pd.to_numeric(item.get("HULLADEK_VESZELYES_10", 0), errors="coerce"),
            "MENNYISEG": pd.to_numeric(item.get("MENNYISEG", 0), errors="coerce"),
        })
    return rows


def expand_multi_categories(df: pd.DataFrame, category_service) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["PRODUCT_GROUP"] = df["HULLADEKKOD"].map(category_service.resolve_groups)
    return df.explode("PRODUCT_GROUP").reset_index(drop=True)


def aggregate_data(raw_df: pd.DataFrame, source_df: pd.DataFrame, category_service) -> Aggregates:
    empty = pd.DataFrame()
    if raw_df is None or raw_df.empty:
        return Aggregates(empty, empty, empty, empty, empty, empty, empty)

    df = raw_df.copy()
    df["MENNYISEG"] = pd.to_numeric(df["MENNYISEG"], errors="coerce").fillna(0)
    df["EV"] = pd.to_numeric(df["EV"], errors="coerce")
    df["HULLADEK_VESZELYES_10"] = pd.to_numeric(df["HULLADEK_VESZELYES_10"], errors="coerce").fillna(0)

    expanded_df = expand_multi_categories(df, category_service)
    if expanded_df.empty:
        expanded_df = df.copy()
        expanded_df["PRODUCT_GROUP"] = "Többi"

    master = (
        expanded_df.groupby(
            [
                "ADATTIPUS",
                "ADATTIPUS_LABEL",
                "CEGNEV",
                "SOURCE_KUJ",
                "EV",
                "HULLADEKKOD",
                "HULLADEK_MEGNEVEZES",
                "HULLADEK_VESZELYES_10",
                "PRODUCT_GROUP",
            ],
            as_index=False,
            dropna=False,
        )["MENNYISEG"]
        .sum()
        .rename(columns={"MENNYISEG": "OSSZES_MENNYISEG"})
    )

    base_unique = (
        master[
            ["CEGNEV", "SOURCE_KUJ", "EV", "HULLADEKKOD", "ADATTIPUS", "ADATTIPUS_LABEL", "OSSZES_MENNYISEG"]
        ]
        .drop_duplicates()
        .copy()
    )

    company_year_category = (
        master.groupby(["CEGNEV", "EV", "PRODUCT_GROUP"], as_index=False)["OSSZES_MENNYISEG"]
        .sum()
    )

    category_totals = (
        master.groupby(["PRODUCT_GROUP"], as_index=False)["OSSZES_MENNYISEG"]
        .sum()
        .sort_values("OSSZES_MENNYISEG", ascending=False)
    )

    year_totals = (
        base_unique.groupby(["EV"], as_index=False)["OSSZES_MENNYISEG"]
        .sum()
        .sort_values("EV")
    )

    company_totals = (
        base_unique.groupby(["CEGNEV"], as_index=False)["OSSZES_MENNYISEG"]
        .sum()
        .sort_values("OSSZES_MENNYISEG", ascending=False)
    )

    if source_df is not None and not source_df.empty:
        src = source_df.copy()
        if "SRC_NETTO_ARBEV" not in src.columns:
            src["SRC_NETTO_ARBEV"] = ""
        if "SRC_SAJAT_TOKE" not in src.columns:
            src["SRC_SAJAT_TOKE"] = ""

        src["SRC_NETTO_ARBEV_NUM"] = parse_hu_numeric(src["SRC_NETTO_ARBEV"])
        src["SRC_SAJAT_TOKE_NUM"] = parse_hu_numeric(src["SRC_SAJAT_TOKE"])

        company_totals = company_totals.merge(
            src[["CEGNEV", "KUJ", "SRC_NETTO_ARBEV", "SRC_NETTO_ARBEV_NUM", "SRC_SAJAT_TOKE", "SRC_SAJAT_TOKE_NUM"]]
            .drop_duplicates(subset=["CEGNEV", "KUJ"]),
            on="CEGNEV",
            how="left"
        )

    return Aggregates(
        raw=df,
        master=master,
        base_unique=base_unique,
        company_year_category=company_year_category,
        company_totals=company_totals,
        category_totals=category_totals,
        year_totals=year_totals,
    )
