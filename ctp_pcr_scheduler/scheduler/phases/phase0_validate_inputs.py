"""
phase0 — validate + normalise inputs (warn-only gate, BTP-MODE).

Builds the canonical lookups every downstream phase uses (item-type map, aging
map in hours with GT cure-by + blank backfill, routing index) and runs the CTP
severity-tagged checks. Never aborts in BTP-MODE; writes a gate report.
"""
from __future__ import annotations
import os
import json
import pandas as pd

import common


def run(ctx: dict, cfg: dict) -> dict:
    drum = ctx["drum"]
    bom = ctx["bom"]
    routing = ctx["routing"]
    itemtype_df = ctx["itemtype_df"]
    aging_df = ctx["aging_df"]

    # --- normalisation: the lookups the pipeline binds to -------------------
    itype_map = common.build_itemtype_map(itemtype_df, bom)
    # NOTE: planning_max_aging_h (tighter shelf-life ceilings) is intentionally NOT
    # applied yet — the BTP-ported values (tread/sidewall 8h, steel belt 6h) need CTP
    # confirmation from the domain experts before tightening the EXPIRED test.
    aging_map = common.build_aging_map(
        aging_df, itype_map, ctx["buffer"], cfg["green_tyre_cure_by_h"])
    routing_idx = common.build_routing_index(routing)
    # Length-rate slitter/cutter duration-basis fix: time on the fed sheet length, not
    # the developed output length (the cap-ply-slitter 154x phantom-bottleneck fix).
    len_input_factor = common.build_length_input_factor(
        routing, bom, float(cfg.get("length_input_min_ratio", 2.0)))
    ctx["itype_map"] = itype_map
    ctx["aging_map"] = aging_map
    ctx["routing_idx"] = routing_idx
    ctx["len_input_factor"] = len_input_factor
    if len_input_factor:
        worst = min(len_input_factor.items(), key=lambda kv: kv[1])
        print(f"[phase0] length-basis fix armed for {len(len_input_factor)} slitter/cutter "
              f"routed_product(s); worst inflation ~{1.0/worst[1]:.0f}x on {worst[0]!r} "
              f"(timed on fed sheet length, not developed output).")

    findings = []

    def add(sev, check, n, detail):
        findings.append({"severity": sev, "check": check, "count": int(n), "detail": detail})

    # --- C: curing cycle-time band (quarantine 0 / 0.1 garbage) -------------
    lo, hi = cfg["cure_min_band"]
    prod = drum[~drum["is_occupancy"]]
    bad_cure = prod[(prod["cure_min"] < lo) | (prod["cure_min"] > hi) | prod["cure_min"].isna()]
    add("FAIL" if len(bad_cure) else "INFO", "curing_cure_min_band", len(bad_cure),
        f"productive blocks with cure_min outside [{lo},{hi}] min")
    ctx["bad_cure_blocks"] = set(bad_cure["block_id"])

    # --- press universe: numeric vs junk tokens -----------------------------
    presses = drum["press_id"].astype(str)
    junk = presses[~presses.str.match(r"^\d+$")]
    add("WARN" if len(junk) else "INFO", "press_universe", junk.nunique(),
        f"non-numeric press tokens: {sorted(junk.unique())[:5]}")

    # --- sku_crosswalk: drum SKUs absent from BOM ---------------------------
    bom_super = set(bom["Super_parent"].unique())
    drum_skus = set(prod["sku"].unique())
    missing_sku = sorted(drum_skus - bom_super)
    add("WARN" if missing_sku else "INFO", "sku_crosswalk", len(missing_sku),
        f"plan SKUs not in BOM (cannot explode): {missing_sku}")
    ctx["unschedulable_skus"] = set(missing_sku)

    # --- mpq coverage: produced item-types with no MPQ floor ----------------
    produced_types = {itype_map.get(c, "UNKNOWN") for c in bom["child"].unique()}
    mpq_types = set(ctx["mpq"].keys())
    missing_mpq = sorted(t for t in produced_types
                         if t.upper() not in mpq_types and t not in ("UNKNOWN", common._GREEN_TYRE))
    add("WARN" if missing_mpq else "INFO", "mpq_coverage", len(missing_mpq),
        f"produced types with no MPQ row: {missing_mpq[:12]}")

    # --- produced intermediates missing routing (SILENT production gap) ------
    # An item that appears as a BOM Parent (it is produced) AND as a child (it is
    # consumed) within the slice SKUs, but has no routing op, is silently dropped
    # from scheduling — its consumer (e.g. the green tyre) is then built without it
    # ever being made or sequenced. Raw leaves (consumed only) are fine to omit.
    slice_skus = ctx.get("slice_skus")
    sb = bom[bom["Super_parent"].isin(slice_skus)] if slice_skus else bom
    produced = set(sb["Parent"].unique())
    consumed = set(sb["child"].unique())
    intermediates = {x for x in (produced & consumed) if x}
    missing_routing = sorted(i for i in intermediates if i not in routing_idx)
    ctx["intermediates_missing_routing"] = set(missing_routing)
    add("WARN" if missing_routing else "INFO", "produced_item_missing_routing",
        len(missing_routing),
        f"produced intermediates with NO routing op (silently unscheduled, "
        f"their assemblies build without them): {missing_routing[:12]}")

    # --- BOM edges with missing child_quantity (silent zero-demand) ----------
    # A NaN child_quantity means phase1b adds zero demand for that child. For a raw
    # leaf that's harmless, but for a PRODUCED child (has routing) it silently drops
    # the item from the schedule and orphans its consumer (becomes a DAG root).
    sbq = sb[["Parent", "child", "child_quantity"]].copy()
    nan_edges = sbq[sbq["child_quantity"].isna()]
    nan_produced = sorted({c for c in nan_edges["child"].unique()
                           if c and c in routing_idx})
    add("WARN" if nan_produced else "INFO", "bom_missing_child_quantity",
        len(nan_produced),
        f"produced items with NaN child_quantity (silently zero-demanded, orphan "
        f"their consumers): {nan_produced[:12]}")

    # --- NOS sub-assembly under-explosion (bead-type convention gap) ---------
    # The BOM is pre-exploded (per-tyre absolute) for the mass chain, but some NOS
    # count sub-assemblies are entered per-PARENT: e.g. a carcass needs 2 apexes, each
    # apex needs 1 bundle -> 2 bundles/tyre, yet the bundle row may read 1. Flag NOS
    # parents whose count >1 feeding a NOS child whose count is < the parent's, so the
    # demand-explosion convention can be confirmed with the bead room (do NOT guess).
    nos = sb[(sb["child_Unit"].astype(str).str.upper().isin(["NOS", "NO"]))]
    cqv = pd.to_numeric(nos["child_quantity"], errors="coerce")
    child_cnt = dict(zip(nos["child"], cqv))
    under = sorted({c for p, c, q in zip(nos["Parent"], nos["child"], cqv)
                    if child_cnt.get(p, 1) and child_cnt.get(p, 1) > 1
                    and q is not None and not pd.isna(q) and q < child_cnt.get(p, 1)})
    add("WARN" if under else "INFO", "nos_subassembly_underexplosion", len(under),
        f"NOS children possibly under-exploded vs parent count (confirm bead-room "
        f"convention; not auto-multiplied): {under[:12]}")

    # --- drum press vs routing-authorised CURING machines (op 200) -----------
    # A drum block whose press_id is NOT in the SKU's routing CURING (op 200)
    # machine list is a phantom/unauthorised press (e.g. the 6009 conflict). It
    # schedules onto a press the routing never sanctioned for that SKU. WARN-level in
    # BTP-MODE (never abort); surfaces the conflict automatically per SKU.
    auth = {}                                        # finished_product -> set(authorised presses)
    is_curing_op = (routing["operation_name"].astype(str).str.upper().str.contains("CURING")
                    | (pd.to_numeric(routing.get("operation_seq"), errors="coerce") == 200))
    for _, rr in routing[is_curing_op].iterrows():
        fp = str(rr.get("routed_product") or rr.get("finished_product") or "").strip()
        if not fp:
            continue
        mp = {m.strip() for m in str(rr["machines"]).replace('"', "").split(",")
              if m.strip() and m.strip().lower() != "nan"}
        auth.setdefault(fp, set()).update(mp)
    unauth = []                                      # (sku, press) pairs off the authorised list
    for sku, press in zip(prod["sku"].astype(str), prod["press_id"].astype(str)):
        allowed = auth.get(sku.strip())
        if allowed is not None and press.strip() and press.strip() not in allowed:
            unauth.append((sku.strip(), press.strip()))
    unauth_uniq = sorted(set(unauth))
    add("WARN" if unauth_uniq else "INFO", "drum_press_not_authorised", len(unauth_uniq),
        f"drum CURING press not in routing op-200 machine list "
        f"(phantom/unauthorised press): {unauth_uniq[:12]}")

    # --- aging min <= max ----------------------------------------------------
    inv = [(c, v) for c, v in aging_map.items() if v[1] is not None and v[0] > v[1]]
    add("FAIL" if inv else "INFO", "aging_min_gt_max", len(inv),
        f"codes with MinAging > MaxAging: {[c for c, _ in inv][:8]}")

    # --- green-tyre cure-by sanity ------------------------------------------
    gt_codes = [c for c, t in itype_map.items() if t == common._GREEN_TYRE]
    bad_gt = [c for c in gt_codes if aging_map.get(c, (0, 0))[1] != cfg["green_tyre_cure_by_h"]]
    add("FAIL" if bad_gt else "INFO", "green_tyre_cureby", len(bad_gt),
        f"GREEN_TYRE codes not pinned to {cfg['green_tyre_cure_by_h']}h: {len(bad_gt)}")

    # --- write the gate report ----------------------------------------------
    n_fail = sum(1 for f in findings if f["severity"] == "FAIL")
    n_warn = sum(1 for f in findings if f["severity"] == "WARN")
    gate = {"verdict": "FAIL" if n_fail else "PASS", "mode": "BTP-MODE (warn-only)",
            "n_fail": n_fail, "n_warn": n_warn, "findings": findings,
            "green_tyre_codes": len(gt_codes)}
    out_dir = ctx["outputs_dir"]
    with open(os.path.join(out_dir, "phase0_gate.json"), "w", encoding="utf-8") as fh:
        json.dump(gate, fh, indent=2, default=str)
    pd.DataFrame(findings).to_csv(os.path.join(out_dir, "phase0_findings.csv"), index=False)

    print(f"[phase0] verdict={gate['verdict']} fail={n_fail} warn={n_warn} "
          f"| GT codes={len(gt_codes)} | bad cure blocks={len(bad_cure)} "
          f"| unschedulable SKUs={len(missing_sku)}")
    return ctx
