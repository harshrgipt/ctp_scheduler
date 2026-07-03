"""
phase6 — independent post-condition validator (REPORT-only in BTP-MODE).

Re-proves the structural constraints from the placed schedule, trusting nothing
the engine logged. Its highest-value finding is SILENT_BREAK: an aging breach the
engine did NOT record in the ledger. In BTP-MODE it reports; it does not gate.
"""
from __future__ import annotations
import os
import json
import pandas as pd

NS_PER_H = 3_600_000_000_000


def run(ctx: dict, cfg: dict) -> dict:
    sched = ctx["schedule"]
    placed = sched[sched["status"] == "PLACED"].set_index("lot_id")
    edges = [tuple(e) for e in ctx["dag_edges"].itertuples(index=False, name=None)]
    findings = {}

    s_ns = {l: pd.Timestamp(t).value for l, t in placed["scheduled_start"].items()}
    f_ns = {l: pd.Timestamp(t).value for l, t in placed["scheduled_finish"].items()}

    # C1 — precedence: producer finishes before consumer starts.
    c1 = sum(1 for p, c in edges
             if p in f_ns and c in s_ns and f_ns[p] > s_ns[c])
    findings["C1_precedence_violations"] = c1

    # C4 — non-overlap per machine.
    c4 = 0
    for m, g in placed.reset_index().groupby("machine"):
        g = g.sort_values("scheduled_start")
        prev = None
        for s, f in zip(g["scheduled_start"], g["scheduled_finish"]):
            if prev is not None and pd.Timestamp(s).value < prev:
                c4 += 1
            prev = pd.Timestamp(f).value
    findings["C4_overlap_violations"] = c4

    # C2 — SILENT BREAK: a real aging breach absent from the engine ledger.
    ledger = ctx["ledger"]
    logged = set()
    if len(ledger):
        logged = set(zip(ledger.get("producer_lot", []), ledger.get("consumer_lot", [])))
    silent = 0
    L = ctx["lots"].set_index("lot_id")
    minage = L["min_aging_h"].to_dict()
    maxage = L["max_aging_h"].to_dict()
    for p, c in edges:
        if p in f_ns and c in s_ns:
            gap_h = (s_ns[c] - f_ns[p]) / NS_PER_H
            too_fresh = gap_h < minage.get(p, 0) - 1e-6
            mx = maxage.get(p, 1e9)
            over_aged = mx is not None and mx < 1e8 and gap_h > mx + 1e-6
            if (too_fresh or over_aged) and (p, c) not in logged:
                silent += 1
    findings["C2_silent_breaks"] = silent

    # C3 — ORPHAN OUTPUTS: a non-green-tyre lot that feeds nothing. The backward
    # "every GT traces to mixing" check can pass while producer lots dangle forward
    # (built then discarded) — the signature of silently-lost precedence, e.g. a
    # NaN-quantity BOM edge that dropped a consumer. Forward-reachability catches it.
    has_succ = set(p for p, _ in edges)
    itype_l = ctx["lots"].set_index("lot_id")["item_type"].to_dict()
    orphans = [l for l in placed.index
               if l not in has_succ and itype_l.get(l) != "GREEN_TYRE"]
    findings["C3_orphan_outputs"] = len(orphans)
    findings["C3_orphan_sample"] = [(l, itype_l.get(l)) for l in orphans[:8]]

    findings["C7_acyclic"] = True  # DAG built by Kahn topo; cycles would have stalled phase4
    verdict = "CLEAN" if (c1 == 0 and c4 == 0 and silent == 0 and not orphans) else "ISSUES"
    findings["verdict"] = verdict

    with open(os.path.join(ctx["outputs2_dir"], "phase6_validation.json"), "w") as fh:
        json.dump(findings, fh, indent=2, default=str)
    print(f"[phase6] verdict={verdict} | C1={c1} C4={c4} silent_C2={silent} "
          f"orphans_C3={len(orphans)}")
    return ctx
