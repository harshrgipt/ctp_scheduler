"""
io_utils.py — input loaders for the CTP PCR scheduler (BTP V6_wave port).

Every reader here is quote-aware (pandas), never str.split(','): the routing
`machines` / `input_components_from_bom` columns embed quoted comma-lists, and a
naive split corrupts proc_time_UOM and manufactures thousands of fake violations.

The drum adapter normalises the LP curing plan (an .xlsx of Excel-serial dates)
into the canonical contract every downstream phase binds to.
"""
from __future__ import annotations
import os
import math
import yaml
import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
EXCEL_EPOCH = "1899-12-30"   # Excel 1900 date system (leap-bug-corrected origin)


# --------------------------------------------------------------------------- #
# Config + path resolution
# --------------------------------------------------------------------------- #
def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_repo_root"] = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    return cfg


def resolve(cfg: dict, rel: str) -> str:
    return os.path.join(cfg["_repo_root"], rel)


# --------------------------------------------------------------------------- #
# The DRUM — fixed curing plan (demand signal)
# --------------------------------------------------------------------------- #
def read_drum(path: str, tz: str = IST) -> pd.DataFrame:
    """Read the LP curing schedule and emit the canonical drum contract.

    Returns one row per *productive* curing block:
        block_id, press_id, sku, qty, cure_min, start_ts, end_ts, shift,
        date, gt_inventory, is_occupancy
    CHANGEOVER / MOULD rows are dropped from demand but their press-time is kept
    as is_occupancy=True rows (qty=0) so phase5 can block the press.
    """
    raw = pd.read_excel(path, sheet_name="Shift Schedule", engine="openpyxl")
    raw.columns = [str(c).strip() for c in raw.columns]

    df = pd.DataFrame()
    df["press_id"] = raw["Machine"].astype(str).str.strip()
    df["sku"] = raw["SKUCode"].astype(str).str.strip()
    df["shift"] = raw["Shift"].astype(str).str.strip()
    df["qty"] = pd.to_numeric(raw["Qty"], errors="coerce").fillna(0.0)
    df["cure_min"] = pd.to_numeric(raw["CycleTime_min"], errors="coerce")
    df["gt_inventory"] = pd.to_numeric(raw.get("GT_Inventory", 0), errors="coerce").fillna(0.0)
    df["remarks"] = raw.get("Remarks", "").astype(str).str.strip()

    # StartTime/EndTime arrive as Excel serial floats (days since 1899-12-30).
    df["start_ts"] = _excel_to_dt(raw["StartTime"], tz)
    df["end_ts"] = _excel_to_dt(raw["EndTime"], tz)
    df["date"] = _excel_to_dt(raw["Date"], tz).dt.tz_localize(None).dt.normalize()

    # Reserved press-occupancy rows: SKU == CHANGEOVER / MOULD_CLEANING / blank.
    sku_up = df["sku"].str.upper()
    occ = sku_up.isin(["CHANGEOVER", "MOULD_CLEANING", "MOULD_CLEAN", "NAN", ""])
    df["is_occupancy"] = occ | (df["qty"] <= 0)

    df = df[df["start_ts"].notna() & df["end_ts"].notna()].reset_index(drop=True)

    # Stable canonical block ids in plan-row order (phase1.5/3 mirror this).
    df.insert(0, "block_id", [f"B{ i:05d}" for i in range(len(df))])
    return df


def _excel_to_dt(series: pd.Series, tz: str) -> pd.Series:
    """Convert an Excel-serial / datetime column to a tz-aware IST Series.

    openpyxl already returns Timestamps for date-formatted cells; only truly
    numeric (serial) columns need the epoch conversion.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        dt = pd.to_datetime(series, errors="coerce")
    else:
        s = pd.to_numeric(series, errors="coerce")
        if s.notna().mean() > 0.5:                   # numeric serials
            dt = pd.to_datetime(s, unit="D", origin=EXCEL_EPOCH)
        else:                                        # date strings
            dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")


def make_plan_params(drum: pd.DataFrame, cfg: dict) -> dict:
    """Synthesize the one-row plan-params control header from the drum horizon."""
    return {
        "plan_id": "CTP_PCR_CuringSchedule",
        "plant": "CTP",
        "product": "PCR",
        "plan_start": drum["start_ts"].min(),
        "plan_end": drum["end_ts"].max(),
        "default_oee": cfg.get("oee", 1.0),
        "timezone": cfg.get("timezone", IST),
    }


# --------------------------------------------------------------------------- #
# Master data (CSV) — all quote-aware via pandas
# --------------------------------------------------------------------------- #
def read_bom(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    df["child_quantity"] = pd.to_numeric(df["child_quantity"], errors="coerce")
    df["Parent_qty"] = pd.to_numeric(df.get("Parent_qty", 1), errors="coerce").fillna(1.0)
    for c in ("Super_parent", "grand_parent", "Parent", "child", "child_Unit", "Equipment"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df


def read_routing(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    for c in ("proc_time", "batch_size", "transfer_time_min", "efficiency",
              "operation_seq", "alt_machine_count"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("finished_product", "routed_product", "operation_name", "department",
              "Equipment", "machines", "proc_time_UOM", "batch_UNIT"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df


def read_aging(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    df["MaxAging"] = pd.to_numeric(df["MaxAging"], errors="coerce")
    df["MinAging"] = pd.to_numeric(df["MinAging"], errors="coerce")
    df["ItemCode"] = df["ItemCode"].astype(str).str.strip()
    return df


def read_itemtype(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    df["ItemCode"] = df["ItemCode"].astype(str).str.strip()
    df["ItemType"] = df["ItemType"].astype(str).str.strip()
    return df


def read_buffer(path: str) -> dict:
    """item-type (lowercased) -> buffer coverage hours."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    out = {}
    for _, r in df.iterrows():
        hrs = pd.to_numeric(r["Buffer Level (Hrs)"], errors="coerce")
        if not pd.isna(hrs):
            out[r["Item type"].strip().lower()] = float(hrs)
    return out


# --------------------------------------------------------------------------- #
# MPQ master (MOQ xlsx) — per-item-type lot-sizing floor, normalised
# --------------------------------------------------------------------------- #
# Representative compound batch floors (KG). Manual Appendix B: master 3x230=690,
# final 3x225=675. Refined per mixer-pick in Slice 2.
_COMPOUND_FLOOR_KG = {"MASTER COMPOUND": 690.0, "FINAL COMPOUND": 675.0}


def read_mpq(path: str) -> dict:
    """Parse the PCR sheet into {item_type: (min_qty, uom)} with lengths in MM.

    Compounds expressed as "N Batches" map to representative KG floors; the
    "Spool/Dipped Roll length(300)" cell maps to 300 MTR; MTR/M floors convert
    to MM so they match phase1b's length normalisation.
    """
    raw = pd.read_excel(path, sheet_name="PCR", engine="openpyxl", header=0)
    floors: dict[str, tuple[float, str]] = {}
    for _, r in raw.iterrows():
        itype = str(r.iloc[0]).strip()
        if not itype or itype.lower().startswith(("for mixers", "machine code", "nan")):
            break  # reached the mixer table / footer
        minq_raw = str(r.iloc[1]).strip() if len(r) > 1 else ""
        uom = str(r.iloc[3]).strip() if len(r) > 3 and not _isblank(r.iloc[3]) else \
              (str(r.iloc[2]).strip() if len(r) > 2 else "")
        qty, norm_uom = _parse_mpq_floor(itype, minq_raw, uom)
        if qty is not None:
            floors[itype.upper()] = (qty, norm_uom)
    return floors


def _parse_mpq_floor(itype: str, minq_raw: str, uom: str):
    up = itype.upper()
    if up in _COMPOUND_FLOOR_KG or "batch" in minq_raw.lower():
        return _COMPOUND_FLOOR_KG.get(up, None), "KG"
    if "length(" in minq_raw.lower():               # Spool/Dipped Roll length(300)
        m = "".join(ch for ch in minq_raw if ch.isdigit())
        return (float(m) * 1000.0 if m else None), "MM"
    val = pd.to_numeric(minq_raw, errors="coerce")
    if pd.isna(val):
        return None, uom
    u = uom.upper()
    if u in ("MTR", "M", "MM"):                       # normalise length → MM
        return float(val) * (1.0 if u == "MM" else 1000.0), "MM"
    if u in ("NOS", "NO"):
        u = "NOS"
    return float(val), (u or "NOS")


# --------------------------------------------------------------------------- #
# Transfer time (xlsx) — component -> minutes, mapped onto item-types
# --------------------------------------------------------------------------- #
# Component abbreviations in the master -> canonical item-types they cover.
_TRANSFER_ITEMTYPE = {
    "IL": ["INNER LINER"], "SW": ["SIDEWALL"], "PLY": ["PLY", "RUBBERIZED PLY"],
    "BEAD": ["BEAD BUNDLE", "BEAD APEX", "APEX", "BEAD WIRE"],
    "TRAED": ["TREAD"], "TREAD": ["TREAD"],
    "BELT": ["STEEL BELT", "RUBBERIZED STEEL BELT", "STEEL BELT EDGE STRIP"],
    "CAPSTRIP": ["CAP STRIP"], "FINAL COMPOUND": ["FINAL COMPOUND"],
    "MASTER COMPOUND": ["MASTER COMPOUND"], "CHAFFER": ["CHAFER", "CHAFFER"],
    "STC": ["STEEL TYRE CORD"], "SP": ["SHOULDER PAD"],
}


def read_transfer(path: str, default_min: float = 10.0) -> dict:
    """item-type (UPPER) -> transfer minutes (PCR line)."""
    raw = pd.read_excel(path, sheet_name=0, engine="openpyxl")
    raw.columns = [str(c).strip() for c in raw.columns]
    comp_col, pcr_col = raw.columns[0], raw.columns[1]
    out = {"_default": default_min}
    for _, r in raw.iterrows():
        comp = str(r[comp_col]).strip().upper()
        mins = pd.to_numeric(r[pcr_col], errors="coerce")
        if comp in _TRANSFER_ITEMTYPE and not pd.isna(mins):
            for itype in _TRANSFER_ITEMTYPE[comp]:
                out[itype.upper()] = float(mins)
    return out


def transfer_for(transfer: dict, item_type: str) -> float:
    return transfer.get((item_type or "").upper(), transfer["_default"])


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _isblank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() in ("", "nan")


def ceil_div(a: float, b: float) -> int:
    return int(math.ceil(a / b)) if b else 0
