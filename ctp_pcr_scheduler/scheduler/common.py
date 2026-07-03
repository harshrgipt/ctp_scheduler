"""
common.py — shared normalisation + cycle-time math for the CTP PCR scheduler.

Pure, deterministic helpers used across phases: item typing (with the documented
GT/CAP fixes), aging-unit normalisation, the routing index, and op_duration_min.
"""
from __future__ import annotations
import math
import re
import pandas as pd

# Spelling/encoding variants → canonical item-type (B7-style canonicalisation).
_TYPE_ALIASES = {
    "synethic rubber": "Synthetic rubber",
    "zince oxide": "Zinc oxide",
    "bead wire": "Bead Wire",
}
_GREEN_TYRE = "GREEN_TYRE"


def canon_item_type(code: str, raw_type: str | None, descr: str | None = None) -> str:
    """Resolve an item code to its controlled type, applying the documented fixes.

    GT* → GREEN_TYRE (B11); CAP* → Cap Strip; alias map for spelling drift;
    else the itemtype-master value; else a description-based fallback.
    """
    c = (code or "").strip()
    if c.upper().startswith("GT ") or re.match(r"^GT[\s\-]?\d", c.upper()):
        return _GREEN_TYRE
    if c.upper().startswith("CAP"):
        return "Cap Strip"
    t = (raw_type or "").strip()
    if t:
        return _TYPE_ALIASES.get(t.lower(), t)
    d = (descr or "").strip()
    return _TYPE_ALIASES.get(d.lower(), d) if d else "UNKNOWN"


def aging_to_hours(value, unit) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    u = str(unit or "Hours").strip().lower()
    v = float(value)
    if u.startswith("day"):
        return v * 24.0
    if u.startswith("min"):
        return v / 60.0
    return v  # hours (default)


def build_itemtype_map(itemtype_df: pd.DataFrame, bom_df: pd.DataFrame) -> dict:
    """code -> canonical item-type, covering BOM codes the master omits."""
    raw = dict(zip(itemtype_df["ItemCode"], itemtype_df["ItemType"]))
    descr = {}
    if "child" in bom_df.columns and "child_description" in bom_df.columns:
        descr = dict(zip(bom_df["child"], bom_df["child_description"]))
    codes = set(raw) | set(bom_df["child"].unique()) | set(bom_df["Parent"].unique())
    return {c: canon_item_type(c, raw.get(c), descr.get(c)) for c in codes}


def build_aging_map(aging_df: pd.DataFrame, itype_map: dict, buffer_h: dict,
                    green_cure_by_h: float, planning_max: dict | None = None) -> dict:
    """code -> (min_h, max_h). GREEN_TYRE forced to [0, cure_by]; blanks backfilled.

    planning_max (item-type -> hours) tightens the max-aging ceiling to the plant's
    planning limit (e.g. silica Final Compound ages out at 48h, not the 120h master
    value) so lot consolidation and the EXPIRED test use the real shelf life.
    """
    planning_max = {k.upper(): float(v) for k, v in (planning_max or {}).items()}
    out: dict[str, tuple[float, float]] = {}
    for _, r in aging_df.iterrows():
        code = r["ItemCode"]
        mn = aging_to_hours(r["MinAging"], r.get("MinAgingUnit"))
        mx = aging_to_hours(r["MaxAging"], r.get("MaxAgingUnit"))
        out[code] = (mn if mn is not None else 0.0, mx)
    # GREEN_TYRE cure-by + backfill missing max from buffer-master type default.
    for code, itype in itype_map.items():
        if itype == _GREEN_TYRE:
            out[code] = (0.0, green_cure_by_h)
            continue
        mn, mx = out.get(code, (0.0, None))
        if mx is None:
            mx = buffer_h.get(itype.lower())          # plant buffer as fallback ceiling
        pm = planning_max.get(itype.upper())
        if pm is not None:                            # cap to the real planning shelf life
            mx = pm if mx is None else min(mx, pm)
        out[code] = (mn if mn is not None else 0.0, mx if mx is not None else 1e9)
    return out


def build_routing_index(routing_df: pd.DataFrame) -> dict:
    """routed_product -> primary operation meta (machines parsed to a list)."""
    idx: dict[str, dict] = {}
    for _, r in routing_df.iterrows():
        rp = r["routed_product"]
        if not rp or rp in idx:
            continue
        machines = [m.strip() for m in str(r["machines"]).split(",") if m.strip()
                    and m.strip().lower() != "nan"]
        idx[rp] = {
            "operation_name": r.get("operation_name", ""),
            "department": r.get("department", ""),
            "machines": machines,
            "proc_time": r.get("proc_time"),
            "proc_time_UOM": r.get("proc_time_UOM", ""),
            "batch_size": r.get("batch_size"),
            "efficiency": r.get("efficiency"),
        }
    return idx


# Length-rate proc UOMs whose duration must be timed on the length the machine
# physically FEEDS, not the developed output length wound at the builder.
_LENGTH_RATE_UOMS = {"MPM", "M/MIN", "MTR/MIN", "MM/MIN"}
# Length child_Units in the BOM (metres/millimetres). MT is a tonne (mass) -> excluded.
_BOM_LENGTH_UNITS = {"M", "MTR", "MM"}


def _bom_len_m(qty, unit) -> float | None:
    """A BOM child_quantity in metres, or None if the unit is not a length."""
    if qty is None or (isinstance(qty, float) and math.isnan(qty)):
        return None
    u = str(unit or "").strip().upper()
    if u in ("M", "MTR"):
        return float(qty)
    if u == "MM":
        return float(qty) / 1000.0
    return None


def build_length_input_factor(routing_df: pd.DataFrame, bom_df: pd.DataFrame,
                              min_ratio: float = 2.0) -> dict:
    """routed_product -> (fed_len_m / developed_len_m) for length-rate slitter/cutter ops.

    THE CAP-PLY-SLITTER FIX (confirmed root cause): a length-rate op (M/MIN etc.) is
    timed on its routed_product's DEMAND quantity — the developed strip length wound at
    the builder (e.g. CAP 66 - CAPSTRIP = 25.761 m/tyre). But the machine physically
    feeds the smaller SHEET/mother-roll named in the routing's input_components_from_bom
    (CAP 66-MOTHERROLL = 0.1668 m/tyre). Timing on the developed length inflates the op
    ~154x -> phantom bottleneck. This returns a per-routed-product scale factor (<1) to
    multiply the duration qty by, so the op is timed on the length actually processed.

    Guarded to fire ONLY where a length op's routed_product has a BOM child that is
    (a) named in input_components_from_bom, (b) a length item (M/MTR/MM), and
    (c) smaller than the routed_product's own developed length by >= min_ratio.
    This excludes mixers, calenders and extruders (compound KG inputs -> no length
    child) and any op where input length == output length (ratio 1) — only the genuine
    developed-output-vs-fed-sheet gap is corrected.

    NB: the factor is a DURATION basis only; it does NOT change the demand quantity the
    builder consumes (still the full developed strip).
    """
    # routed_product -> its developed per-tyre length as a BOM child (max across SKUs).
    dev_len: dict[str, float] = {}
    for c, q, u in zip(bom_df["child"], bom_df["child_quantity"], bom_df["child_Unit"]):
        lm = _bom_len_m(q, u)
        if lm and lm > 0:
            code = str(c).strip()
            if lm > dev_len.get(code, 0.0):
                dev_len[code] = lm
    # (parent, child) -> child per-tyre length as a BOM child of that parent (min across SKUs).
    fed_len: dict[tuple, float] = {}
    for p, c, q, u in zip(bom_df["Parent"], bom_df["child"],
                          bom_df["child_quantity"], bom_df["child_Unit"]):
        lm = _bom_len_m(q, u)
        if lm and lm > 0:
            k = (str(p).strip(), str(c).strip())
            fed_len[k] = min(lm, fed_len.get(k, lm))

    out: dict[str, float] = {}
    seen = set()
    for r in routing_df.itertuples():
        rp = str(getattr(r, "routed_product", "")).strip()
        pu = str(getattr(r, "proc_time_UOM", "")).strip().upper()
        if not rp or rp in seen or pu not in _LENGTH_RATE_UOMS:
            continue
        seen.add(rp)
        out_len = dev_len.get(rp)
        if not out_len:
            continue
        inp_raw = str(getattr(r, "input_components_from_bom", "") or "")
        inp_names = {x.strip() for x in re.split(r"[,;]", inp_raw) if x.strip()}
        best_in = None
        for name in inp_names:                       # a length child fed into this rp
            il = fed_len.get((rp, name))
            if il and il > 0 and (best_in is None or il < best_in):
                best_in = il
        if best_in is None or best_in <= 0:
            continue
        if out_len / best_in >= min_ratio:           # genuine developed>>fed gap
            out[rp] = best_in / out_len              # duration scale factor (<1)
    return out


def op_duration_min(qty: float, qty_uom: str, proc_uom: str,
                    proc_time: float, batch_size: float, eff: float = 1.0) -> float:
    """Per-lot duration in minutes — the documented UOM-dispatched cycle-time math.

    qty_uom is the item's demand unit (MM/KG/NOS); proc_uom selects the formula.
    Length quantities (MM) convert to metres for rate/per-unit-minute families.
    """
    e = eff if (eff and eff > 0) else 1.0
    pu = (proc_uom or "").upper().strip()
    qu = (qty_uom or "").upper().strip()
    pt = float(proc_time) if proc_time and proc_time > 0 else 0.0
    bs = float(batch_size) if batch_size and batch_size > 0 else 0.0
    if pt == 0:
        return 1.0
    metres = qty / 1000.0 if qu == "MM" else qty

    if pu in ("MPM", "M/MIN", "MTR/MIN", "MM/MIN"):           # line-speed rate
        return max(metres / pt / e, 1.0)
    if "BATCH" in pu:                                          # per-BATCH family
        # MIN/BATCH = minutes per batch; SEC/BATCH = seconds per batch (÷60 to minutes).
        per_batch_min = pt if "MIN" in pu else pt / 60.0
        if bs > 0:
            return max(math.ceil(qty / bs) * per_batch_min / e, 1.0)
        return max(qty * per_batch_min / e, 1.0)              # per-piece
    if pu == "SEC":
        if bs > 0:
            return max((qty / bs) * pt / 60.0 / e, 1.0)
        return max(qty * pt / 60.0 / e, 1.0)
    if pu in ("MIN", "MINS"):                                 # per-unit minutes
        return max(metres * pt / e, 1.0)
    if pu in ("RPM", "REV/MIN", "PC/HR", "PCS/HR", "PCH"):    # bead-line throughput
        # The "RPM" UOM tag on Bead Apex/Bundle ops is a master-data mislabel: the
        # value is a throughput in PIECES PER HOUR (200/128.89 = 1.55 min/bead;
        # 1000 beads -> 7.76 h, the plausible CTP bead-line band). The old fallback
        # read it as minutes-per-piece and produced 36-year schedules. Confirm the
        # UOM with the routing master-data owner; re-tag "RPM" -> "PC/HR".
        return max(qty / pt * 60.0 / e, 1.0)                  # pieces/hour -> minutes
    if pu in ("CUTS/MIN", "NOS/MIN", "NO/MIN", "PC/MIN", "PCS/MIN"):
        # Per-MINUTE throughput tags on the cutters (belt/ply: CUTS/MIN) and the bead
        # apexer (NOS/MIN): proc_time is PIECES PRODUCED PER MINUTE, so duration =
        # qty / rate. Read as minutes-per-piece (the old fallback) it inflated the belt
        # cutter ~216x and the bead apexer ~26x — overloading those machines >10x and
        # blowing the makespan to 650+ days. NB: "R/MIN" (bead winding, revolutions/min
        # against a KG demand) is deliberately NOT handled here — it needs a
        # revs-per-bead / KG-per-rev factor confirmed by the CTP bead room before
        # encoding; until then it stays caught by the phase2 runaway cap.
        return max(qty / pt / e, 1.0)                         # pieces/minute -> minutes
    return max(qty * pt / e, 1.0)                             # fallback per-piece min
