"""
pipeline.py — shared orchestration for the CTP PCR scheduler.

Both the CLI (run.py) and the Streamlit dashboard (app.py) drive the phases
through here so they behave identically. Exposes:

    load_context(cfg, drum_override=None)  -> ctx           (reads all inputs)
    select_slice_skus(ctx, cfg)            -> [sku, ...]
    PHASES                                  -> [(key, title, module), ...]
    run_phase(ctx, cfg, module)            -> (ok, stdout, error_traceback)
    kpis(ctx, cfg)                         -> dict
    phase_artifacts(cfg, keys)             -> {filename: path}

Every phase is run inside a stdout capture + try/except so a failure in one
phase is surfaced (with its traceback) instead of killing the whole process —
this is what makes the dashboard's phase-by-phase debugging possible.
"""
from __future__ import annotations
import os
import io as _io
import sys
import glob
import traceback
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_PHASES_DIR = os.path.join(HERE, "phases")
if _PHASES_DIR not in sys.path:
    sys.path.insert(0, _PHASES_DIR)

import io_utils as io
import phase0_validate_inputs as p0
import phase1b_demand_explosion as p1b
import phase1_5_wave_builder as p15
import phase2_lot_sizing as p2
import phase3_dag_construction as p3
import phase4_cpm as p4
import phase5_forward_placement as p5
import phase6_validate as p6

# (key, human title, module) in execution order.
PHASES = [
    ("phase0",  "Phase 0 — Validate & normalise inputs",   p0),
    ("phase1b", "Phase 1b — Demand explosion (BOM walk)",   p1b),
    ("phase1_5", "Phase 1.5 — Wave builder",                p15),
    ("phase2",  "Phase 2 — Lot sizing",                     p2),
    ("phase3",  "Phase 3 — DAG construction",               p3),
    ("phase4",  "Phase 4 — CPM time windows",               p4),
    ("phase5",  "Phase 5 — Forward placement",              p5),
    ("phase6",  "Phase 6 — Post-condition validation",      p6),
]


def load_config(config_path: str | None = None) -> dict:
    return io.load_config(config_path or os.path.join(HERE, "config.yaml"))


def load_context(cfg: dict, drum_override: str | None = None) -> dict:
    """Read every input and build the base context. drum_override (an absolute
    path to an uploaded curing schedule) replaces the configured drum file."""
    ctx = {
        "outputs_dir": io.resolve(cfg, cfg["outputs_dir"]),
        "outputs2_dir": io.resolve(cfg, cfg["outputs2_dir"]),
    }
    os.makedirs(ctx["outputs_dir"], exist_ok=True)
    os.makedirs(ctx["outputs2_dir"], exist_ok=True)

    inp = cfg["inputs"]
    drum_path = drum_override or io.resolve(cfg, inp["drum"])
    ctx["drum_path"] = drum_path
    ctx["drum"] = io.read_drum(drum_path, cfg["timezone"])
    ctx["bom"] = io.read_bom(io.resolve(cfg, inp["bom"]))
    ctx["routing"] = io.read_routing(io.resolve(cfg, inp["routing"]))
    ctx["aging_df"] = io.read_aging(io.resolve(cfg, inp["aging"]))
    ctx["itemtype_df"] = io.read_itemtype(io.resolve(cfg, inp["itemtype"]))
    ctx["buffer"] = io.read_buffer(io.resolve(cfg, inp["buffer"]))
    ctx["mpq"] = io.read_mpq(io.resolve(cfg, inp["mpq"]))
    ctx["transfer"] = io.read_transfer(io.resolve(cfg, inp["transfer"]))
    ctx["plan_params"] = io.make_plan_params(ctx["drum"], cfg)
    return ctx


def select_slice_skus(ctx: dict, cfg: dict) -> list:
    """Top-N schedulable SKUs by total committed qty (deterministic).

    slice.only_skus (a list) pins the run to exactly those SKUs, overriding the
    top-N pick — use it to audit a specific, complete-data SKU.
    """
    drum, bom = ctx["drum"], ctx["bom"]
    schedulable = set(bom["Super_parent"].unique())
    only = cfg["slice"].get("only_skus")
    if only:
        return [s for s in only if s in schedulable]
    prod = drum[~drum["is_occupancy"]]
    tot = (prod[prod["sku"].isin(schedulable)]
           .groupby("sku")["qty"].sum().reset_index()
           .sort_values(["qty", "sku"], ascending=[False, True]))
    if cfg["slice"].get("enabled"):
        tot = tot.head(cfg["slice"]["max_skus"])
    return list(tot["sku"])


def run_phase(ctx: dict, cfg: dict, module) -> tuple[bool, str, str | None]:
    """Run one phase, capturing its stdout. Returns (ok, stdout_text, traceback)."""
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            module.run(ctx, cfg)
        return True, buf.getvalue(), None
    except Exception:
        return False, buf.getvalue(), traceback.format_exc()


def kpis(ctx: dict, cfg: dict) -> dict:
    """The honest KPI block: placed count paired with the aging-CLEAN rate."""
    sched, ledger = ctx.get("schedule"), ctx.get("ledger")
    if sched is None:
        return {}
    placed = sched[sched["status"] == "PLACED"]
    n_placed = len(placed)
    # OPENING_WIP_REQUIRED is a documented day-0 inventory boundary condition, not a
    # schedulable breach — exclude it from the breach set and the clean-rate.
    real = ledger
    opening_wip = 0
    if ledger is not None and len(ledger):
        is_wip = ledger["type"] == "OPENING_WIP_REQUIRED"
        opening_wip = int(is_wip.sum())
        real = ledger[~is_wip]
    breach_lots = set()
    if real is not None and len(real):
        breach_lots = set(real.get("producer_lot", [])) | set(real.get("consumer_lot", []))
    clean = sum(1 for l in placed["lot_id"] if l not in breach_lots)
    out = {
        "lots_placed": int(n_placed),
        "pinned_curing": int((sched["status"] == "PINNED").sum()),
        "unplaced": int((sched["status"] == "UNPLACED").sum()),
        "aging_clean_rate_pct": round(100.0 * clean / n_placed, 1) if n_placed else 0.0,
        "real_breaches": int(len(real)) if real is not None else 0,
        "opening_wip_required": opening_wip,
    }
    if real is not None and len(real):
        out["breach_by_type"] = {str(k): int(v) for k, v in real["type"].value_counts().items()}
    if n_placed:
        ms = sched["scheduled_finish"].max() - sched["scheduled_start"].min()
        out["makespan"] = str(ms)
    return out


def phase_artifacts(cfg: dict, key: str) -> dict:
    """Map output filenames -> absolute paths for a phase key (both output dirs)."""
    out = {}
    for d in (io.resolve(cfg, cfg["outputs_dir"]), io.resolve(cfg, cfg["outputs2_dir"])):
        for path in sorted(glob.glob(os.path.join(d, f"{key}*"))):
            out[os.path.basename(path)] = path
    return out
