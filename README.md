# Location Automation — Retailer Location Migration

A single, self-contained application that migrates a **fresh retailer extract**
forward by carrying over the RDS system identifiers — `rds_point_id`,
`rds_outlet_id`, `rds_route_id`, `rds_cluster_id` — from the previous,
already-mapped extract, resolving them through the `ecrm.locations` hierarchy
(point → route → cluster → outlet).

New locations that cannot be resolved from the old file are inserted into the
database automatically (PostgreSQL) and back-filled into the CSV.

## Structure

```
.
├─ Location_Automation_App/
│   └─ Location_Automation_full.py   # The ENTIRE app — one file, no other files needed
├─ README.md
└─ .gitignore
```

`Location_Automation_full.py` is fully self-contained:
- glass (PySide6) GUI — translucent frameless window, rounded glass panels
- pipeline backend (config, lookups, DB insert + `RETURNING id` read-back, all
  5 migration stages, validation, reconciliation, verification)
- "Graphify"-style context memory persisted to `context_memory.json`
- startup dependency gate (installs pandas / psycopg2 / PySide6 if missing)

## Run it

```bash
cd Location_Automation_App
python Location_Automation_full.py
```

On startup it checks for `pandas`, `psycopg2`, and `PySide6`; if any are missing
it asks before installing them (with a progress bar).

## How to use the app

1. **Setup** tab — point to the old mapped file and the fresh extract
   (defaults: `data/retailers_02_05_2026_update.csv`, `data/retailers_05_07_2026.csv`),
   the output file, reports dir, and the PostgreSQL connection.
2. **Run** tab — tick the stages (point → keys → route → cluster → outlet),
   choose **Dry run** (no writes) or **Write to DB**, click **Run pipeline**.
3. **Visuals** tab — bar chart of Matched vs Missing per stage.
4. **Memory** tab — persistent notes + saved-run history (`context_memory.json`).
5. **Log** tab — full live log of the run.

> order note: route and cluster stages run **before** outlet so each new row's
> `parent` is already resolved. New points are out of scope — the app halts and
> flags them for manual review.

## Configuration

`config.json` is optional and git-ignored; the app uses sensible defaults and
reads `config.json` next to the script if present. DB credentials can also come
from environment variables: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
`DB_PASSWORD` (or `DB_DSN`).

## Outputs (`finded_data\`)

- `missing_*.csv` — unmatched rows for manual review
- `new_route_upload.csv`, `new_cluster_upload.csv`, `new_outlet_upload.csv` and
  `*_backfill_map.csv` — inserted rows and the `source_id → new_id` map
- `reconcile_warnings.csv` — route vs cluster cross-check mismatches
- the mapped output CSV

## Safety rules

- Never overwrite the original/master file — only the working output file.
- Only patch rows that are still missing — never overwrite filled id columns.
- Never match clusters by name alone; scope by point/route first.
- Never invent missing values — unresolved rows are flagged for review.
- Inputs are validated before running (required columns, old-file presence,
  duplicate `Outlet_Code`).
- A run with "Write to DB" ends with a verification gate: all four id columns
  must be fully populated, and route/cluster ids are cross-reconciled.
