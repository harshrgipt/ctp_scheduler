"""
CTP PCR Scheduler — command-line runner.

Runs phase0 -> phase6 over the configured inputs and writes the phase artifacts +
the final floor schedule to outputs/. Prints the honest KPI block.

Usage:
    python run.py                          # use config.yaml as-is
    python run.py --sku 1325215813079TUNE3 # pin to one SKU (overrides slice.only_skus)
    python run.py --all                    # schedule ALL SKUs in the curing plan
    python run.py --topn 10                # schedule the top-10 SKUs by demand
    python run.py --wf 0.8                 # override pooled_window_factor (0.2 JIT .. 0.8 pooled)
    python run.py --curing inputs/other_plan.xlsx   # use a different curing plan
"""
from __future__ import annotations
import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
SCHED = os.path.join(HERE, "scheduler")
sys.path.insert(0, SCHED)
sys.path.insert(0, os.path.join(SCHED, "phases"))

import pipeline as P
from make_floor_schedule import build_floor_schedule


def main():
    ap = argparse.ArgumentParser(description="CTP PCR Scheduler")
    ap.add_argument("--sku", help="pin to a single SKU code")
    ap.add_argument("--all", action="store_true", help="schedule all SKUs")
    ap.add_argument("--topn", type=int, help="schedule the top-N SKUs by demand")
    ap.add_argument("--wf", type=float, help="pooled_window_factor override")
    ap.add_argument("--curing", help="path to a curing-plan xlsx (overrides config)")
    args = ap.parse_args()

    cfg = P.load_config(os.path.join(SCHED, "config.yaml"))
    if args.sku:
        cfg["slice"].update(enabled=True, max_skus=1, only_skus=[args.sku])
    if args.topn:
        cfg["slice"].update(enabled=True, max_skus=args.topn, only_skus=None)
    if args.all:
        cfg["slice"].update(enabled=False, only_skus=None)
    if args.wf is not None:
        cfg["pooled_window_factor"] = args.wf
    drum_override = os.path.abspath(args.curing) if args.curing else None

    ctx = P.load_context(cfg, drum_override=drum_override)
    ctx["slice_skus"] = P.select_slice_skus(ctx, cfg)
    print(f"[run] curing plan : {ctx['drum_path']}")
    print(f"[run] SKUs ({len(ctx['slice_skus'])}): {ctx['slice_skus'][:8]}"
          + (" ..." if len(ctx["slice_skus"]) > 8 else ""))
    print("=" * 70)

    for key, title, module in P.PHASES:
        ok, out, tb = P.run_phase(ctx, cfg, module)
        if out:
            print(out.rstrip())
        if not ok:
            print(f"\n[run] ABORT — {key} failed:\n{tb}")
            sys.exit(1)

    print("\n" + "=" * 24 + " KPI BLOCK (honest) " + "=" * 24)
    for k, v in P.kpis(ctx, cfg).items():
        print(f"  {k:24s}: {v}")

    # final floor schedule (reference 15-column format)
    dest = os.path.join(P.io.resolve(cfg, cfg["outputs2_dir"]), "floor_schedule.xlsx")
    build_floor_schedule(
        os.path.join(P.io.resolve(cfg, cfg["outputs2_dir"]), "phase5_schedule_updated.csv"),
        os.path.join(P.io.resolve(cfg, cfg["outputs2_dir"]), "phase3_dag_edges_updated.csv"),
        dest)
    print(f"\n[run] floor schedule -> {dest}")
    print(f"[run] phase artifacts -> {P.io.resolve(cfg, cfg['outputs2_dir'])}")


if __name__ == "__main__":
    main()
