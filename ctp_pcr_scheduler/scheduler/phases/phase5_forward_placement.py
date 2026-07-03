"""
phase5 — forward placement (BTP-MODE: force-place + honest breach ledger).

Every lot is placed onto a concrete machine via a shared MachineTimeline
(bisect slot search, integer-ns, changeover only between differing keys). Bottleneck
mixers run pure-ASAP; terminal builds are pulled ALAP to the curing need_by inside
the 8h cure-by band; everything else is ALAP to its CPM LFT. When a constraint
cannot be honoured the lot is force-placed and the breach is logged, never erased.
"""
from __future__ import annotations
import os
import bisect
import heapq
from collections import defaultdict
import pandas as pd

import common
from io_utils import transfer_for

NS_PER_H = 3_600_000_000_000
NS_PER_S = 1_000_000_000


def _ts_floor_s(value_ns: int, tz: str) -> pd.Timestamp:
    """Build a tz-aware Timestamp floored to whole seconds. The integer-ns slot math
    yields 9-digit fractional seconds that break strict ISO-8601 / SAP OData parsers;
    flooring to seconds keeps timestamps exactly representable without shifting a slot
    past its interval (floor never rounds a start later or a finish earlier)."""
    floored = (int(value_ns) // NS_PER_S) * NS_PER_S
    return pd.Timestamp(floored, tz="UTC").tz_convert(tz)


class MachineTimeline:
    """One machine's occupancy: intervals sorted by start, bisect slot search."""

    def __init__(self):
        self.starts: list[int] = []
        self.iv: list[tuple] = []          # (start, end, key, lot_id) aligned to starts
        self.busy_ns = 0

    def _free_windows(self, key, co_ns, open_ns):
        """Yield (lo, hi) free windows respecting changeover to differing-key neighbours."""
        prev_end, prev_key = open_ns, None
        for (s, e, k, _) in self.iv:
            lo = prev_end + (co_ns if prev_key is not None and prev_key != key else 0)
            hi = s - (co_ns if k != key else 0)
            if hi > lo:
                yield (lo, hi)
            prev_end, prev_key = e, k
        lo = prev_end + (co_ns if prev_key is not None and prev_key != key else 0)
        yield (lo, None)                   # tail window, unbounded above

    def earliest_start(self, after, dur, key, co_ns, open_ns):
        for lo, hi in self._free_windows(key, co_ns, open_ns):
            s = max(after, lo)
            if hi is None or s + dur <= hi:
                return s
        return after

    def latest_start(self, target, dur, key, co_ns, open_ns, floor):
        best = None
        for lo, hi in self._free_windows(key, co_ns, open_ns):
            cap = target if hi is None else min(target, hi - dur)
            if cap >= lo and cap >= floor:
                best = cap if best is None else max(best, cap)
        return best

    def insert(self, start, dur, key, lot_id):
        end = start + dur
        i = bisect.bisect_left(self.starts, start)
        self.starts.insert(i, start)
        self.iv.insert(i, (start, end, key, lot_id))
        self.busy_ns += dur
        return end


# Routing operation_name (UPPER) -> config changeover_min key. The config keys are the
# machine names ("4 Roll Calender"), but lots carry the operation name ("FOUR ROLL
# CALENDAR"); without this map an EXACT lookup missed every key and silently charged the
# 15-min default (calenders got 15 min where the real changeover is 44 -> infeasible slots).
_CHANGEOVER_OP_ALIAS = {
    "FOUR ROLL CALENDAR": "4 Roll Calender",
    "RHC": "Roller Head Calender",
    "EXTRUSION": "Triplex Extruder",
    "PLY CUTTER": "Ply Cutter",
    "BELT CUTTER": "Belt Cutter",
    "GT BUILDING": "Building",
    "CAR BUILDING": "Carcass Building (1st Stage)",
    "CARCASS BUILDING (1ST STAGE)": "Carcass Building (1st Stage)",
    # Ops with no dedicated config changeover value: aliased to themselves so they
    # resolve deterministically (still to `default` since config lacks a key). The
    # aliasing documents them as KNOWN — the _warn_default_changeovers pass below
    # surfaces any op that lands on the 15-min default so a silent default never hides.
    "CAP PLY SLITTER": "Cap Ply Slitter",
    "EDGE GUM": "Edge Gum",
    "BEAD APEXING": "Bead Apexing",
    "BEAD WINDING PCR": "Bead Winding PCR",
}

# One-time guard state: distinct op-names that resolved to the 15-min `default`.
_DEFAULT_CO_WARNED = False


def _resolve_changeover_key(op_name: str):
    """Return (key, is_mixing) — the config lookup key a lot's op resolves to."""
    name = (op_name or "").strip()
    up = name.upper()
    if "MIXING" in up:
        return "mixing", True
    return _CHANGEOVER_OP_ALIAS.get(up, name), False


def _warn_default_changeovers(op_names, cfg) -> None:
    """ONE-TIME data-gap warning: list every distinct operation whose changeover
    resolves to the config `default` (no explicit changeover value). Does NOT invent
    minutes — it just makes a silent default visible so it can be reviewed."""
    import sys
    global _DEFAULT_CO_WARNED
    if _DEFAULT_CO_WARNED:
        return
    co = cfg["changeover_min"]
    default_min = co.get("default", 15.0)
    hit = {}
    for name in set(op_names):
        key, is_mixing = _resolve_changeover_key(name)
        if is_mixing:
            continue
        if key not in co:                                # falls through to default
            hit[(name or "").strip()] = key
    if hit:
        listing = ", ".join(sorted(f"{op!r}->{key!r}" for op, key in hit.items()))
        msg = (f"[phase5][WARN] {len(hit)} operation(s) have NO explicit changeover_min "
               f"and use the {default_min}-min default: {listing}")
        print(msg, file=sys.stderr)
        print(msg)                                       # also to captured stdout
    _DEFAULT_CO_WARNED = True


def _changeover_ns(op_name: str, cfg: dict) -> int:
    co = cfg["changeover_min"]
    key, is_mixing = _resolve_changeover_key(op_name)
    if is_mixing:
        mins = co.get("mixing", 2.0)
    else:
        mins = co.get(key, co.get("default", 15.0))
    return int(mins * 60 * 1e9)


def run(ctx: dict, cfg: dict) -> dict:
    lots = ctx["lots"].set_index("lot_id")
    edges = [tuple(e) for e in ctx["dag_edges"].itertuples(index=False, name=None)]
    lt = ctx["lot_times"].set_index("lot_id")
    transfer = ctx["transfer"]
    ref = ctx["plan_start"]
    open_ns = ref.value - int(cfg["schedule_open_lead_h"] * NS_PER_H)
    cure_by_ns = int(cfg["green_tyre_cure_by_h"] * NS_PER_H)
    buffer_ns = int(cfg.get("pre_curing_buffer_h", 0.0) * NS_PER_H)  # build-finish slack
    no_aest = set(t.upper() for t in cfg["no_aest_item_types"])

    pred = defaultdict(list); succ = defaultdict(list)
    for p, c in edges:
        pred[c].append(p); succ[p].append(c)

    # per-lot static fields
    dur_ns, minage_ns, maxage_ns, tr_ns, co_ns, itype, op, need_ns, lft_ns, machines = ({} for _ in range(10))
    for l in lots.index:
        dur_ns[l] = max(int(float(lots.at[l, "duration_h"]) * NS_PER_H), int(1 * 60 * 1e9))
        minage_ns[l] = int(float(lots.at[l, "min_aging_h"]) * NS_PER_H)
        mx = float(lots.at[l, "max_aging_h"]); maxage_ns[l] = int(min(mx, 1e8) * NS_PER_H)
        itype[l] = lots.at[l, "item_type"]
        tr_ns[l] = int(transfer_for(transfer, itype[l]) * 60 * 1e9)
        op[l] = lots.at[l, "operation"]
        co_ns[l] = _changeover_ns(op[l], cfg)
        need_ns[l] = int(pd.Timestamp(lots.at[l, "need_by"]).value)
        lft_ns[l] = int(pd.Timestamp(lt.at[l, "LFT"]).value)
        machines[l] = [m for m in str(lots.at[l, "machines"]).split(",") if m] or ["UNASSIGNED"]
    _warn_default_changeovers([op[l] for l in lots.index], cfg)  # one-time silent-default guard
    pooled = {l: bool(lots.at[l, "pooled"]) for l in lots.index} if "pooled" in lots.columns \
             else {l: False for l in lots.index}

    # dispatch order: Kahn topo with priority (LFT, item, lot_id) — producers first.
    indeg = {l: len(pred[l]) for l in lots.index}
    heap = [(lft_ns[l], lots.at[l, "item_code"], l) for l in lots.index if indeg[l] == 0]
    heapq.heapify(heap)

    timelines: dict[str, MachineTimeline] = defaultdict(MachineTimeline)
    start_ns, finish_ns, placed_machine, status = {}, {}, {}, {}
    sched_rows, ledger, infeas = [], [], []

    def is_build(l):
        # Only the CURED TERMINAL (green tyre) is pulled ALAP to the press cure-by.
        # Intermediate builds (carcass) must precede it, so they go ALAP-to-LFT like
        # every other component — targeting curing_start would crowd out the green tyre
        # and slip it ~1.6h late every cycle.
        return itype[l] == common._GREEN_TYRE

    while heap:
        _, _, l = heapq.heappop(heap)
        d = dur_ns[l]
        material_ready = max([open_ns] +
                             [finish_ns[p] + tr_ns[p] + minage_ns[p] for p in pred[l]])
        curing_start = need_ns[l]

        if (not pooled[l]) and itype[l].upper() in no_aest:  # non-pooled bottleneck mixers: ASAP
            target, floor, asap = material_ready, material_ready, True
        elif is_build(l):                                    # terminal build: ALAP to cure-by
            # aim to FINISH pre_curing_buffer before the press so finite-capacity
            # contention eats the buffer instead of slipping past curing_start.
            target = curing_start - minage_ns[l] - buffer_ns - d
            floor = max(material_ready, curing_start - cure_by_ns - d)
            asap = False
        else:                                                # everything else: ALAP to LFT
            target = lft_ns[l] - d
            floor = material_ready
            asap = False
        target = max(target, floor)

        # choose machine + slot
        key = lots.at[l, "item_code"]

        def _pick(mode):
            best = None
            for m in sorted(machines[l]):
                tl = timelines[m]
                if mode == "asap":
                    s = tl.earliest_start(material_ready, d, key, co_ns[l], open_ns)
                else:
                    s = tl.latest_start(target, d, key, co_ns[l], open_ns, floor)
                    if s is None:
                        s = tl.earliest_start(material_ready, d, key, co_ns[l], open_ns)
                score = (abs(s - target), tl.busy_ns, m)
                if best is None or score < best[0]:
                    best = (score, m, s)
            return best

        best = _pick("asap" if asap else "alap")
        # terminal green tyre: if ALAP overshoots the press start, fall back to ASAP so
        # the build finishes before curing (build-line contention, not a real shortage).
        if not asap and itype[l] == common._GREEN_TYRE and best[2] + d > curing_start:
            alt = _pick("asap")
            if alt is not None and alt[2] + d < best[2] + d:
                best = alt
        _, m, s = best
        e = timelines[m].insert(s, d, lots.at[l, "item_code"], l)
        start_ns[l], finish_ns[l], placed_machine[l] = s, e, m
        status[l] = "PLACED" if m != "UNASSIGNED" else "UNPLACED"
        if m == "UNASSIGNED":
            infeas.append({"lot_id": l, "item": lots.at[l, "item_code"], "reason": "NO_ELIGIBLE_MACHINE"})

        # commit-test (advisory in BTP-MODE) → breach ledger
        for p in pred[l]:
            gap = s - finish_ns[p]
            gap_avail = gap - tr_ns[p]
            if gap_avail < minage_ns[p] - NS_PER_H // 3600:
                ledger.append(_breach(p, l, lots, gap, minage_ns[p], maxage_ns[p], "TOO_FRESH"))
            elif maxage_ns[p] > 0 and gap > maxage_ns[p] + NS_PER_H // 3600:
                ledger.append(_breach(p, l, lots, gap, minage_ns[p], maxage_ns[p], "OVER_AGED"))
        if is_build(l):
            cure_gap = curing_start - e
            if cure_gap < 0 or cure_gap > cure_by_ns:
                # A negative gap that even an ASAP build (material_ready + d) could
                # not have beaten is not a schedulable miss — the curing block fires
                # before a from-empty chain can deliver. That is an opening-WIP need,
                # not a breach the scheduler can fix; flag it as such (TOC: seed day-0
                # WIP or advance the build-line open).
                earliest_finish = material_ready + d
                if cure_gap > cure_by_ns:
                    kind = "CUREBY_EXPIRED"
                elif earliest_finish > curing_start:
                    kind = "OPENING_WIP_REQUIRED"
                else:
                    kind = "CUREBY_NEGATIVE"
                ledger.append({"producer_lot": l, "consumer_lot": lots.at[l, "block_id"],
                               "item": lots.at[l, "item_code"], "gap_h": cure_gap / NS_PER_H,
                               "min_aging_h": 0.0, "max_aging_h": cfg["green_tyre_cure_by_h"],
                               "type": kind,
                               "delta_h": (cure_gap - cure_by_ns) / NS_PER_H})

        sched_rows.append({
            "lot_id": l, "item": lots.at[l, "item_code"], "item_type": itype[l],
            "sku": lots.at[l, "sku"], "operation": op[l],
            "department": lots.at[l, "department"], "machine": m,
            "scheduled_start": _ts_floor_s(s, cfg["timezone"]),
            "scheduled_finish": _ts_floor_s(e, cfg["timezone"]),
            "duration_h": d / NS_PER_H, "qty": lots.at[l, "qty"], "uom": lots.at[l, "uom"],
            "status": status[l],
        })

        for c in succ[l]:
            indeg[c] -= 1
            if indeg[c] == 0:
                heapq.heappush(heap, (lft_ns[c], lots.at[c, "item_code"], c))

    # echo the curing drum as PINNED rows
    drum = ctx["drum"]
    pinned = drum[(~drum["is_occupancy"]) & (drum["block_id"].isin(ctx["slice_blocks"]))]
    for _, r in pinned.iterrows():
        sched_rows.append({
            "lot_id": f"CURE_{r['block_id']}", "item": r["sku"], "item_type": "CURING",
            "sku": r["sku"], "operation": "Curing", "department": "Curing",
            "machine": r["press_id"],
            "scheduled_start": r["start_ts"].floor("s"),
            "scheduled_finish": r["end_ts"].floor("s"),
            "duration_h": (r["end_ts"] - r["start_ts"]) / pd.Timedelta(hours=1),
            "qty": r["qty"], "uom": "NOS", "status": "PINNED"})

    schedule = pd.DataFrame(sched_rows).sort_values(["scheduled_start", "lot_id"]).reset_index(drop=True)
    ledger_df = pd.DataFrame(ledger)
    util = pd.DataFrame([{"machine": m, "booked_h": tl.busy_ns / NS_PER_H,
                          "n_lots": len(tl.iv)} for m, tl in sorted(timelines.items())])

    o2 = ctx["outputs2_dir"]
    schedule.to_csv(os.path.join(o2, "phase5_schedule_updated.csv"), index=False)
    ledger_df.to_csv(os.path.join(o2, "phase5_aging_violations_updated.csv"), index=False)
    pd.DataFrame(infeas).to_csv(os.path.join(o2, "phase5_infeasibility_updated.csv"), index=False)
    util.to_csv(os.path.join(o2, "phase5_machine_utilization_updated.csv"), index=False)

    ctx["schedule"] = schedule
    ctx["ledger"] = ledger_df
    placed = (schedule["status"] == "PLACED").sum()
    print(f"[phase5] placed={placed} pinned={(schedule['status']=='PINNED').sum()} "
          f"unplaced={(schedule['status']=='UNPLACED').sum()} | breaches={len(ledger_df)} "
          f"| machines used={len(util)}")
    return ctx


def _breach(p, c, lots, gap, mn, mx, kind):
    return {"producer_lot": p, "consumer_lot": c, "item": lots.at[p, "item_code"],
            "gap_h": gap / NS_PER_H, "min_aging_h": mn / NS_PER_H,
            "max_aging_h": mx / NS_PER_H, "type": kind,
            "delta_h": (mn - gap) / NS_PER_H if kind == "TOO_FRESH" else (gap - mx) / NS_PER_H}
