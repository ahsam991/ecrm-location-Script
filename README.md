# Retailer Location Migration — Automated Pipeline

Migrate a **fresh retailer extract** forward by carrying over the RDS system
identifiers from the previous, already-mapped extract. Four ids — `rds_point_id`,
`rds_outlet_id`, `rds_route_id`, `rds_cluster_id` — are resolved through the
`ecrm.locations` hierarchy (point → route → cluster → outlet). Rows that cannot be
resolved from the old file are genuinely new locations and are inserted into the
database automatically, then back-filled into the CSV.

---

## Repository structure

```
.
├─ New Script/                 # The automation (Python code)
│   ├─ config.py               # Paths, DB DSN, type codes
│   ├─ config.example.json     # Sample config (copy to config.json)
│   ├─ migration_common.py     # Lookups, normalization, DB insert + read-back, stages
│   ├─ run_pipeline.py         # CLI orchestrator
│   ├─ gui_pipeline.py         # Simple tkinter GUI
│   ├─ Location_Automation_full.py  # Glass (PySide6) single-file GUI + Graphify memory
│   ├─ Install_Library.py      # One-shot dependency installer with a progress bar
│   ├─ 1..5.*.py               # The 5 pipeline stages (standalone wrappers)
│   └─ requirements.txt
└─ Old Ref/                    # Reference & documentation
    ├─ location_migration_pipeline.md        # Runbook / lessons learned
    ├─ location_migration_pipeline_PLAN.md   # Full implementation plan
    ├─ Location_Migration_Pipeline_Documentation.docx
    ├─ document/                             # Older duplicate docs
    └─ finded_data/                          # Example reports + reference xlsx
```

## The pipeline

The orchestrator runs stages **top-down** so each new row's `parent` is already
resolved:

| Order | Stage | Resolves | Join key | New row parent |
|-------|-------|----------|----------|----------------|
| 1 | point | `rds_point_id` | `point_id` | (new points → halt) |
| 2 | keys  | builds `point_rt_iden`, `point_cl_rt_iden`, `point_cl_iden` | — | — |
| 3 | route | `rds_route_id` | `route_section_id` | `rds_point_id` |
| 4 | cluster | `rds_cluster_id` (+ route cross-check) | `point_cl_rt_iden` | `rds_route_id` |
| 5 | outlet | `rds_outlet_id` | `Outlet_Code` | `rds_cluster_id` |

Each stage:
1. **Matches** existing rows from the old file and fills the id column.
2. For **missing** rows, inserts into `ecrm.locations` (PostgreSQL auto-increment +
   `INSERT ... RETURNING id`) and reads back `source_id → new_id`.
3. **Back-fills** the CSV only on still-missing rows (the golden rule).

> order note: run route (`4`) and cluster (`5`) **before** outlet (`3`). A new
> outlet's parent is its (already-resolved) cluster id.

## Setup

Install the dependencies (pandas, psycopg2-binary, python-dotenv; plus PySide6 for
the glass GUI):

```bash
python Install_Library.py
```

> `Location_Automation_full.py` also performs its own dependency check on startup
> and asks before installing anything missing.

## Configuration

Copy `config.example.json` to `config.json` and adjust paths / database:

```bash
cp New\ Script\config.example.json New\ Script\config.json
```

Defaults (also overridable via environment `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`
or `DB_DSN`):

| Field | Default | Purpose |
|-------|---------|---------|
| `old_file` | `data/retailers_02_05_2026_update.csv` | previous mapped extract |
| `new_file` | `data/retailers_05_07_2026.csv` | fresh extract |
| `output_file` | `data/retailers_05_07_2026_update.csv` | mapped output |
| `save_dir` | `finded_data` | audit / report CSVs |
| `table` | `ecrm.locations` | destination table |

Drop your real extracts into `data\` (it is git-ignored).

## Usage

Command line:

```bash
cd "New Script"
python run_pipeline.py --dry-run     # validate counts, no writes
python run_pipeline.py --write-db    # insert new rows + backfill + verify
```

Only one stage:

```bash
python "4.update_rds_route_id.py" --write-db
```

GUIs:

```bash
python gui_pipeline.py               # lightweight tkinter GUI
python Location_Automation_full.py   # glass (PySide6) GUI + Graphify memory
```

## Outputs (`finded_data\`)

- `missing_*.csv` — rows that had no match (manual-review list)
- `new_route_upload.csv`, `new_cluster_upload.csv`, `new_outlet_upload.csv` and
  `*_backfill_map.csv` — what was inserted and the `source_id → new_id` map
- `reconcile_warnings.csv` — rows where `rds_route_id` disagrees with the cluster
  cross-check
- the final mapped output at `data\retailers_05_07_2026_update.csv`

## Safety rules

- **Never overwrite the original/master file** — only the working output file.
- **Only patch missing rows** — never overwrite already-filled id columns.
- **Never match clusters by name alone** — scope by point/route first.
- **Never invent missing values** — unresolved rows are flagged for review.
- **New points are out of scope** — the pipeline halts; review manually.
- **Input validation** runs before stages: required columns, old-file presence,
  duplicate `Outlet_Code`.

## Verification

A `--write-db` run ends with a gate: all four id columns must be fully populated
(`isna().sum() == 0`), and route/cluster ids are cross-reconciled.

## References

- `Old Ref/location_migration_pipeline.md` — full runbook & lessons learned
- `Old Ref/location_migration_pipeline_PLAN.md` — the implementation plan
- `Old Ref/Location_Migration_Pipeline_Documentation.docx` — documentation
