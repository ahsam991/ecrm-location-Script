# Location Automation — Retailer Location Migration

A single, self-contained application that migrates a **fresh retailer extract**
forward by carrying over the RDS system identifiers from the previous,
already-mapped extract, resolving them through the `ecrm.locations` hierarchy.

Every month a new retailer extract arrives from ManushTech. It contains every
outlet/route/cluster/point in the country but **does not** know the internal
RDS ids that the `ecrm` system uses. This application fills in those ids by
reusing the mapping from the previous, already-mapped extract, and automatically
creates brand-new locations in the database. The output is a fully-mapped CSV.

---

## Repository structure

```
.
├─ Location_Automation_App/
│   └─ Location_Automation_full.py   # The ENTIRE application (one file)
├─ README.md                         # This document
└─ .gitignore
```

`Location_Automation_full.py` is fully self-contained (no other code files
needed). It contains the glass GUI, the migration logic, the database layer,
input validation, and a dependency installer.

---

## The core data concept

There are two data sources:

| | File (default) | What it is |
|---|---|---|
| **Old / master** | `data/retailers_02_05_2026_update.csv` | The previous extract that was **already mapped** — it already has all four RDS ids filled. This is the baseline. |
| **New / update** | `data/retailers_05_07_2026.csv` | The freshly received extract. It has the raw ManushTech fields but **no** RDS ids yet. |

The new file must be matched against the old/master file. The matching key is
the **record identity**:

```
Point + Route + Cluster   ->   unique record (the "identifier")
```

| Identifier column | Built from | Used for |
|---|---|---|
| `point_rt_iden` | `rds_point_id` + `Point` + `Route/Sec` | point + route identity |
| `point_cl_rt_iden` | `point_id` + `route_section_id` + `Cluster_Id` | point + route + cluster identity (the strict key) |
| `point_cl_iden` | `rds_point_id` + `Point` + cleaned `Cluster_Name` | point + cluster identity |

Cluster names are normalised before comparison (`"Zone-A"` and `"zone a"`
collapse to the same key) so name-only mismatches do not create false "new"
records.

---

## The 5 migration stages

The app runs stages **top-down** so that when a new row is inserted, its
`parent` id is already known:

| Order | Stage | Resolves | Join key | New row's parent |
|---|---|---|---|---|
| 1 | **Point** | `rds_point_id` | `point_id` | — (new points → halt for manual review) |
| 2 | **Keys** | builds the 3 identifier columns | — | — |
| 3 | **Route** | `rds_route_id` | `route_section_id` | `rds_point_id` |
| 4 | **Cluster** | `rds_cluster_id` | `point_cl_rt_iden` | `rds_route_id` |
| 5 | **Outlet** | `rds_outlet_id` | `Outlet_Code` | `rds_cluster_id` |

### What a stage does
1. **Load** the old and new files.
2. **Match** every row of the new file against the old file using the stage's
   join key. Matched rows get the RDS id copied over.
3. **Detect missing** rows — new records that have no match in the old file.
4. For each missing row the app **builds an insert** for `ecrm.locations`:
   - `name` = the display name from the CSV
   - `parent` = the already-resolved id one level up
   - `type` = 6 (route), 7 (cluster), 8 (outlet)
   - `source_id` = the original raw ManushTech id (for traceability)
5. **Insert** all new rows in one transaction using PostgreSQL auto-increment
   and `INSERT ... RETURNING id`, then read back the new ids.
6. **Back-fill** the CSV — only the still-missing rows are patched with the new
   ids (never overwrite already-filled ids).

---

## How the record flows (visual)

```
OLD MAPPED FILE  +  NEW RAW EXTRACT
            \            /
             \          /
          MATCH BY IDENTIFIER
            |            |
    +-------+            +-------+
    |                          |
  MATCHED                  NOT MATCHED
    |                        (genuinely new)
    |                          |
 copy RDS id              build insert row
    |                    (name, parent, type, source_id)
    |                          |
    |                   INSERT into ecrm.locations
    |                   (PostgreSQL auto-increment)
    |                          |
    |                   RETURNING id -> read back new id
    |                          |
    +----------+---------------+
               |
         TOTAL DATA CSV
      (every row has all 4 ids)
               |
         VERIFICATION GATE
   (rds_point_id, rds_outlet_id,
    rds_route_id, rds_cluster_id
    all filled -> pass)
```

---

## Run the application

```bash
cd Location_Automation_App
python Location_Automation_full.py
```

On startup it checks that `pandas`, `psycopg2`, and `PySide6` are installed. If
any are missing it **asks you first**, then installs them with a progress bar.

### The GUI screens

| Screen | What you do |
|---|---|
| **Setup** | Browse/choose the old mapped file, the new extract, the output file and the reports folder (a `⋯` button next to every field opens the file dialog). Enter the PostgreSQL connection. Save/load a `config.json`. |
| **Run** | Tick the stages you want, choose **Dry run** (no writes) or **Write to DB**, click **Run pipeline**. Progress bar + live status. |
| **Visuals** | Bar chart of *Matched vs Missing* per stage after a run. |
| **Log** | Full live log of the run — every stage, every insert, every warning. |

### Dry run vs real run

| Mode | What happens |
|---|---|
| **Dry run** (default) | Loads and matches the files, reports how many rows are matched vs missing per stage. **Nothing is written** — no DB inserts, no CSV output. Use this to sanity-check a new extract. |
| **Write to DB** | Does the dry-run work **plus** inserts new locations into `ecrm.locations`, back-fills the CSV, and runs the verification gate. |

---

## Validation & safety rules

These are enforced automatically:

1. **Never overwrite the original/master file** — only the working output file is written.
2. **Only patch missing rows** — already-filled id columns are never overwritten.
3. **Never match clusters by name alone** — scoped by point + route + cluster.
4. **Never invent missing values** — unresolved rows are flagged, not fabricated.
5. **Input validation** runs before the stages: required columns exist, the old
   file exists, `Outlet_Code` has no duplicates.
6. **New points are out of scope** — if a genuinely new point appears the app
   halts and tells you to review manually (its parent chain is undefined).
7. **Duplicate protection** — `source_id`s that already exist in the database
   abort the insert to prevent duplicates.
8. **Verification gate** — a real run only reports success when all four id
   columns are fully populated.
9. **Reconciliation** — `rds_route_id` (route stage) is cross-checked against
   the cluster stage's route; mismatches are reported in
   `reconcile_warnings.csv`.

---

## Outputs (written to the reports folder, default `finded_data\`)

| File | Meaning |
|---|---|
| `missing_*.csv` | Rows that had no match in the old file (manual-review list) |
| `new_route_upload.csv`, `new_cluster_upload.csv`, `new_outlet_upload.csv` | The rows inserted into the database (with new `parent` + `type`) |
| `*_backfill_map.csv` | The `source_id → new_id` map used to back-fill the CSV |
| `reconcile_warnings.csv` | Route vs cluster cross-check mismatches |
| `data\..._update.csv` | The final fully-mapped output CSV |

---

## Configuration

`config.json` is optional and git-ignored. The app reads it if present; it can
also be created/saved from the **Setup** screen. Sensible defaults apply.
Database credentials can come from the config or environment variables:
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (or a full `DB_DSN`).

### `ecrm.locations` insert shape

| Column | Value |
|---|---|
| `name` | display name from the CSV |
| `parent` | resolved id of the parent level |
| `type` | 6 = route, 7 = cluster, 8 = outlet |
| `source_id` | original raw ManushTech id |
| `is_deleted` / `active` | `FALSE` / `TRUE` |
| `created_at` / `updated_at` | `now()` |
| `id` | database auto-increment |
