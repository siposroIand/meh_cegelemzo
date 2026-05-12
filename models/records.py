from dataclasses import dataclass
import pandas as pd


@dataclass
class Aggregates:
    raw: pd.DataFrame
    master: pd.DataFrame
    base_unique: pd.DataFrame
    company_year_category: pd.DataFrame
    company_totals: pd.DataFrame
    category_totals: pd.DataFrame
    year_totals: pd.DataFrame