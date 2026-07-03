"""
Floor-schedule formatter — converts the phase5 schedule into the plant's reference
15-column floor-schedule layout, with:
  * shift-crossing jobs split at 07:00/15:00/23:00 (qty time-proportioned, residual on
    the last segment so segment qtys sum exactly),
  * FG_SKU_CODE / FG_DESCRIPTION from each lot's SKU,
  * source_lot_id carrying the phase5 lot id for traceability.
Works for single-SKU or multi-SKU runs.
"""
from __future__ import annotations
import re
from datetime import timedelta
import pandas as pd

BOUND_HOURS = (7, 15, 23)


def _shift_of(ts):
    h = ts.hour
    return "A" if 7 <= h < 15 else ("B" if 15 <= h < 23 else "C")


def _plant_day(ts):
    return (ts - pd.Timedelta(hours=7)).normalize()


def _next_boundary(t):
    c = [t.replace(hour=h, minute=0, second=0, microsecond=0) for h in BOUND_HOURS
         if t.replace(hour=h, minute=0, second=0, microsecond=0) > t]
    c.append((t + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0))
    return min(c)


def _split(s, e, qty, is_nos):
    segs, cur = [], s
    while cur < e:
        se = min(e, _next_boundary(cur))
        segs.append((cur, se)); cur = se
    tot = (e - s).total_seconds() or 1.0
    out, acc = [], 0
    for i, (a, b) in enumerate(segs):
        if i < len(segs) - 1:
            q = round(qty * (b - a).total_seconds() / tot)
            q = int(q) if is_nos else round(q, 1); acc += q
        else:
            q = qty - acc; q = int(round(q)) if is_nos else round(q, 1)
        out.append((a, b, q))
    return out


def build_floor_schedule(schedule_csv: str, edges_csv: str, dest_xlsx: str) -> str:
    src = pd.read_csv(schedule_csv)
    prod = src[src["status"].isin(["PLACED", "PINNED"])].copy()
    prod["start_time"] = pd.to_datetime(prod["scheduled_start"], format="ISO8601").dt.tz_localize(None).dt.floor("s")
    prod["end_time"] = pd.to_datetime(prod["scheduled_finish"], format="ISO8601").dt.tz_localize(None).dt.floor("s")
    prod["machine"] = prod["machine"].astype(str).apply(lambda m: re.sub(r'[\"\s]', "", m))
    prod["item_type"] = prod["item_type"].replace({"GREEN_TYRE": "Green Tyres", "CURING": "Cured Tyre"})

    rows = []
    for r in prod.itertuples(index=False):
        is_nos = str(r.uom).upper() == "NOS"
        q0 = int(round(r.qty)) if is_nos else round(float(r.qty), 1)
        for si, (a, b, q) in enumerate(_split(r.start_time, r.end_time, q0, is_nos)):
            rows.append({"date": _plant_day(a), "machine": r.machine, "shift": _shift_of(a),
                         "item": r.item, "item_type": r.item_type, "process": r.operation,
                         "department": r.department, "start_time": a, "end_time": b,
                         "produce_qty": q, "UOM": r.uom, "FG_SKU_CODE": r.sku,
                         "_base": r.lot_id, "_seg": si, "_n": None})
    df = pd.DataFrame(rows)
    df["_n"] = df.groupby("_base")["_seg"].transform("max") + 1
    df = df.sort_values(["start_time", "item", "machine"]).reset_index(drop=True)
    df["_iseq"] = df.groupby("item")["_base"].transform(lambda s: pd.factorize(s)[0] + 1)
    df["lot_id"] = df.apply(
        lambda r: f"{r['item']}_L{int(r['_iseq']):04d}" + (f"#{chr(65 + int(r['_seg']))}" if r["_n"] > 1 else ""),
        axis=1)
    df["source_lot_id"] = df["_base"]
    df["FG_DESCRIPTION"] = pd.NA

    out = df[["date", "machine", "shift", "item", "item_type", "process", "department",
              "start_time", "end_time", "produce_qty", "UOM", "lot_id", "source_lot_id",
              "FG_SKU_CODE", "FG_DESCRIPTION"]]
    piv = (out.groupby(["machine", "date", "shift", "UOM"], as_index=False)["produce_qty"]
              .sum().rename(columns={"produce_qty": "Sum of produce_qty"}))
    with pd.ExcelWriter(dest_xlsx, engine="openpyxl",
                        datetime_format="yyyy-mm-dd hh:mm:ss", date_format="yyyy-mm-dd") as xw:
        out.to_excel(xw, sheet_name="Schedule", index=False)
        piv.to_excel(xw, sheet_name="MachineDayShift", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col if c.value is not None), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(w + 2, 40)
    return dest_xlsx
