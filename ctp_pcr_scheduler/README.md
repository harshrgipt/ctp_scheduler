# CTP PCR Production Scheduler

A deterministic, phase-based finite-capacity scheduler for **JK Tyre CTP Chennai — PCR
(passenger car radial)**. It takes a fixed **curing plan** (the demand signal), explodes it
through the **BOM**, sizes lots, builds a precedence **DAG**, computes **CPM** time windows,
and **force-places** every lot onto a concrete machine — honouring precedence, machine
non-overlap, changeovers and rubber **shelf-life (aging)** windows. It emits a shop-floor
schedule plus an honest KPI block (placed count paired with the aging-clean rate and the
breach ledger — never "placed %" alone).

The curing presses are treated as the **drum** (the fixed bottleneck): their plan is pinned,
and everything upstream is scheduled to feed them just in time.

---

## 1. Quick start

```bash
# 1. install dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. run with the bundled example (1 SKU, sample data in inputs/)
python run.py
```

Outputs are written to `outputs/`. The headline deliverable is
`outputs/floor_schedule.xlsx`.

### Run options
```bash
python run.py                          # use config.yaml as-is (1 pinned SKU)
python run.py --sku 1325215813079TUNE3 # pin to a single SKU
python run.py --topn 10                # schedule the top-10 SKUs by demand
python run.py --all                    # schedule every SKU in the curing plan
python run.py --wf 0.8                 # pooled batching (multi-SKU); 0.2 = near-JIT (single SKU)
python run.py --curing inputs/other_plan.xlsx   # use a different curing plan
```

**Tip:** use `--wf 0.2` for a single SKU (fewest aging breaches) and `--wf 0.8` for
multi-SKU / full-plan runs (compounds pooled across SKUs, far fewer batches).

---

## 2. The phases

The pipeline runs eight phases in order (`scheduler/pipeline.py` → `PHASES`). Each runs in a
stdout+traceback capture so a failure is surfaced, not swallowed.

| Phase | File | What it does |
|------|------|--------------|
| **0 — Validate & normalise** | `phase0_validate_inputs.py` | Reads all inputs, builds the item-type map, aging map (hours), and routing index; arms the cap-ply length-basis fix; runs severity-tagged data checks (bad cure times, SKUs missing BOM, produced-items-missing-routing, unauthorised curing presses, etc.). Warn-only — never aborts. Writes `phase0_gate.json`. |
| **1b — Demand explosion** | `phase1b_demand_explosion.py` | Walks each SKU's BOM (already pre-exploded: `child_quantity` is absolute per-tyre) to a per-tyre rate for every item, scales by each curing block's qty. Normalises length units (M/MTR → MM). |
| **1.5 — Wave builder** | `phase1_5_wave_builder.py` | Slices the horizon into fixed 3-day waves anchored on the 07:00 plant-day frame. |
| **2 — Lot sizing** | `phase2_lot_sizing.py` | Two grains: **build items** (green tyre/carcass) stay per-block; **pooled items** (compounds, plies, belts, beads) are consolidated into time-phased batches on a consumption clock, window = shelf-life × `pooled_window_factor`. Applies whole-batch rounding for compounds and the length-basis (fed-sheet) duration factor. |
| **3 — DAG construction** | `phase3_dag_construction.py` | Wires producer→consumer precedence edges across the lot consolidation. |
| **4 — CPM** | `phase4_cpm.py` | Forward/backward critical-path pass → EST/LFT time windows per lot (infinite-capacity). |
| **5 — Forward placement** | `phase5_forward_placement.py` | Places every lot on a concrete machine via a bisect slot-search timeline: bottleneck mixers ASAP, terminal green-tyre ALAP to the press cure-by, everything else ALAP to its LFT. Applies changeovers between differing item keys. Force-places on constraint conflict and logs an honest breach (never erased). Echoes the pinned curing blocks. |
| **6 — Post-condition validate** | `phase6_validate.py` | Independently re-proves precedence (C1), machine non-overlap (C4), silent aging breaks (C2), orphan outputs (C3), acyclicity. Report-only. |

`run.py` then formats the placed schedule into `floor_schedule.xlsx`
(`scheduler/make_floor_schedule.py`).

---

## 3. Inputs (`inputs/`)

| File | Format | Purpose |
|------|--------|---------|
| `curing_schedule.xlsx` | xlsx, sheet **`Shift Schedule`** | The curing plan / demand signal. Columns: `Date, Shift, Machine, SKUCode, StartTime, EndTime, Qty, CycleTime_min, GT_Inventory, Remarks`. Each productive row = a pinned press block. |
| `bom.csv` | csv | Bill of materials. Cols: `Super_parent, Equipment, grand_parent, Parent, Parent_qty, Parent_unit, child, child_quantity, child_Unit, child_description`. **Pre-exploded**: `child_quantity` is absolute per-tyre. |
| `routing.csv` | csv | Per-operation routing. Cols incl. `routed_product, operation_name, department, machines, proc_time, proc_time_UOM, batch_size, input_components_from_bom, efficiency`. |
| `aging_master.csv` | csv | `ItemCode, MinAging, MinAgingUnit, MaxAging, MaxAgingUnit` — shelf-life windows. |
| `itemtype_master.csv` | csv | `ItemCode, ItemType`. |
| `buffer_master.csv` | csv | `Item type, Buffer Level (Hrs)`. |
| `mpq.xlsx` | xlsx, sheet `PCR` | Minimum production quantities / batch floors per item-type. |
| `transfer_time.xlsx` | xlsx | Component → transfer minutes. |

**Note on the curing xlsx header:** if your raw plant file has title rows above the header
(e.g. `SHIFT-WISE SCHEDULE`), keep only the real header row (`Date, Shift, Machine, …`) as
row 1 of the `Shift Schedule` sheet, as in the bundled `curing_schedule.xlsx`.

### `proc_time_UOM` conventions the scheduler understands
- `M/MIN`, `MPM` — line speed; duration = length(m) ÷ rate. (M/MTR BOM values auto-scaled to mm.)
- `CUTS/MIN`, `NOS/MIN` — throughput pieces/min; duration = qty ÷ rate.
- `SEC/BATCH`, `MIN/BATCH` — per-batch; duration = ceil(qty/batch) × proc.
- `SEC`, `MIN` — per piece / per unit.
- `RPM`/`PC/HR` — treated as pieces/hour (documented CTP bead-line mislabel).

---

## 4. Outputs (`outputs/`)

| File | Contents |
|------|----------|
| **`floor_schedule.xlsx`** | The deliverable. Sheet `Schedule`: 15 cols (`date, machine, shift, item, item_type, process, department, start_time, end_time, produce_qty, UOM, lot_id, source_lot_id, FG_SKU_CODE, FG_DESCRIPTION`), shift-crossing jobs split at 07:00/15:00/23:00. Sheet `MachineDayShift`: per machine/day/shift/UOM rollup. |
| `phase0_gate.json` / `phase0_findings.csv` | Data-validation gate + findings. |
| `phase1_demand_updated.csv` | Exploded demand. |
| `phase2_lots_updated.csv` | Sized lots (qty, duration_h, aging window, pooled flag). |
| `phase3_dag_edges_updated.csv` | Precedence edges. |
| `phase4_lot_times_updated.csv` | CPM EST/LFT windows. |
| `phase5_schedule_updated.csv` | Placed schedule (start/finish/machine/status). |
| `phase5_aging_violations_updated.csv` | Honest breach ledger. |
| `phase5_machine_utilization_updated.csv` | Booked hours / lot count per machine. |
| `phase6_validation.json` | Independent post-condition verdict. |

---

## 5. Configuration (`scheduler/config.yaml`)

Key knobs (see the file for all):

| Key | Meaning |
|-----|---------|
| `slice.enabled / max_skus / only_skus` | which SKUs to schedule (pin, top-N, or all) |
| `pooled_window_factor` | batch window vs shelf life: **0.2** ≈ JIT (best aging, single SKU), **0.8** pooled (multi-SKU) |
| `length_input_min_ratio` | arms the cap-ply length-basis fix when output/input length ratio ≥ this (default 2.0) |
| `produce_to_demand` | make-to-demand; MPQ not used to pad qty up |
| `green_tyre_cure_by_h` | green tyre must be cured within this window of building |
| `schedule_open_lead_h` | how early production may start before the plan |
| `changeover_min` | per-operation changeover minutes |
| `planning_max_aging_h` | per-item-type shelf-life ceilings |

---

## 6. Key modeling notes (important, non-obvious)

- **BOM is pre-exploded** — `child_quantity` is the absolute per-tyre quantity. Do **not**
  multiply along the BOM path.
- **Cap-ply slitter length-basis fix** — a slitter is timed on the sheet length it *feeds*
  (`input_components_from_bom`, the mother-roll), **not** the developed strip length wound at
  the builder. Timing on the developed length inflated the op ~154× and created a phantom
  bottleneck. See `scheduler/common.py → build_length_input_factor`.
- **Curing is pinned** — the scheduler does not compute cure duration; it echoes the curing
  plan's press blocks (the drum). Curing presses are the intended bottleneck.
- **Honest KPIs** — the breach ledger is never erased; `phase6` independently re-proves the
  constraints. Aging-clean rate is always reported with the placed count.
- **Data gaps surface, never silently drop** — blank proc_time/machines, unauthorised curing
  presses, unit-collisions, etc. are flagged at phase0/phase5 (WARN), not hidden.

---

## 7. Folder structure

```
ctp_pcr_scheduler/
├── README.md
├── requirements.txt
├── run.py                     # CLI entry point
├── inputs/                    # all input data (swap for your plant files)
│   ├── curing_schedule.xlsx
│   ├── bom.csv
│   ├── routing.csv
│   ├── aging_master.csv
│   ├── itemtype_master.csv
│   ├── buffer_master.csv
│   ├── mpq.xlsx
│   └── transfer_time.xlsx
├── outputs/                   # generated (gitignored)
└── scheduler/
    ├── config.yaml            # all parameters
    ├── pipeline.py            # orchestration (load, slice, run phases, KPIs)
    ├── common.py              # cycle-time math, item typing, aging, length-basis fix
    ├── io_utils.py            # input loaders (quote-aware, UOM-normalising)
    ├── make_floor_schedule.py # floor-schedule formatter
    └── phases/
        ├── phase0_validate_inputs.py
        ├── phase1b_demand_explosion.py
        ├── phase1_5_wave_builder.py
        ├── phase2_lot_sizing.py
        ├── phase3_dag_construction.py
        ├── phase4_cpm.py
        ├── phase5_forward_placement.py
        └── phase6_validate.py
```

---

## 8. Interpreting the KPI block

```
lots_placed          : production lots placed on a machine
pinned_curing        : curing press blocks echoed from the plan
unplaced             : lots with no eligible machine (data gap) — should be 0
aging_clean_rate_pct : % of placed lots with no shelf-life breach — the honesty metric
real_breaches        : shelf-life violations logged (OVER_AGED / TOO_FRESH)
opening_wip_required : blocks whose from-empty chain can't deliver in time (day-0 WIP)
makespan             : span from first production op to last finish
```

A healthy single-SKU run: `unplaced=0`, `aging_clean_rate_pct` ≈ 99–100%,
`opening_wip_required=0`, curing presses ~90–95% utilised, everything upstream well under
100%.

---

## 9. Example result (bundled sample — 1 SKU, `1325215813079TUNE3`)

```
placed=1653  pinned=450  unplaced=0  aging_clean=99.9%  breaches=1  makespan≈22.9 days
curing presses ~94% util (the constraint); building ~19%; all else ≤13%.
```
