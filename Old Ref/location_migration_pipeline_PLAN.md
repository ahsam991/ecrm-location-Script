# Retailer Location Migration — Automated Pipeline Plan

## 1. Complete Project Summary (as-is)

A periodic data-migration task. Each cycle a **fresh** retailer extract (`retailers_05_07_2026.csv`) must be "mapped forward" by carrying four RDS system identifiers from the previous, already-mapped file (`retailers_02_05_2026_update.csv`):

`rds_point_id` → `rds_outlet_id` → `rds_route_id` → `rds_cluster_id`

These proxy the real `ecrm.locations` hierarchy. Rows that cannot be resolved are **genuinely new locations** and today are created manually in `ecrm.locations`, then backfilled into the CSV.

### Repository layout
- `location_migration_pipeline.md` — the authoritative runbook / lessons-learned.
- `location_migration/document/` — duplicate copy of that runbook.
- `location_migration/*.py` — the 5 ordered scripts (see table).
- `location_migration/finded_data/` — outputs: `missing_*.csv`, `new_route_upload.csv`, `new_cluster_upload.csv`, plus 2 manual `.xlsx` reference files.
- `location_migration/venv/` — **empty** (pandas must re-install).
- `data/*.csv` source/input files and `missing_outlet_id.csv` are **NOT in the repo** (large / transient). The `.docx` file duplicates the runbook.

### The 5 scripts (current order)
| # | Script | Resolves | Join key (normalization) | Last run result |
|---|---|---|---|---|
| 1 | `1.point_update_and_find.py` | `rds_point_id` | `point_id` (raw, **no** strip/lower) | 708,748/708,748 = 100% |
| 2 | `2.update_3_identifier_column.py` | builds `point_rt_iden`, `point_cl_rt_iden`, `point_cl_iden` | pure construction | all rows |
| 3 | `3.update_rds_outlet_id.py` | `rds_outlet_id` | `Outlet_Code` (strip+lower) | 708,746 matched, **2 missing** |
| 4 | `4.update_rds_route_id.py` | `rds_route_id` | `route_section_id` (exact) | 696,764 matched, **11,984 missing** |
| 5 | `5.update_composite_cluster_route_id.py` | `rds_cluster_id` (+ route cross-check) | `point_cl_rt_iden` (strip) | 696,763 matched, **11,985 missing** |

Every script: load `data/retailers_05_07_2026_update.csv` (running output), build a lookup from the OLD file, map the id, write unmatched rows to `finded_data/<name>.csv`.

### Key columns built in File 2
| Column | Built from (normalized) | Purpose |
|---|---|---|
| `point_rt_iden` | `rds_point_id` + `Point` + `Route/Sec` (strip+lower) | point+route identity (RDS-side) |
| `point_cl_rt_iden` | `point_id` + `route_section_id` + `Cluster_Id` (raw ManushTech, **no normalize**) | the strict key used in File 5 |
| `point_cl_iden` | `rds_point_id` + `Point` + cleaned `Cluster_Name` | point+cluster identity (RDS-side) |

`clean_cluster`: trim, strip all whitespace, `-`→`_`, lowercase (so `"Zone-A"` and `"zone a"` collapse).

### Manual gap-filling (the recurring recipe, hierarchy top-down)
Starting ids observed in `finded_data` (proves single `MAX(id)` snapshot discipline):
- max(id) = `1830441` before this run; new **routes** = `1830442..1830605` (165), new **clusters** = `1830606..1832971` (2,366), new **outlets** = `1830440/1830441` (Foodi, The Cozy Bean).

1. Find unmatched rows (`isna`).
2. Build an insert-ready file: new `id`, `name` from CSV, `parent` = already-resolved id one level up, `type` (route=6, cluster=7, outlet=8), `source_id` = original raw id, `is_deleted=FALSE`, `active=TRUE`, `created_at=updated_at=now()`.
3. Insert into `ecrm.locations`.
4. Read back new ids by `source_id`.
5. Backfill the CSV **only on still-missing rows** (golden rule), keyed on the join key (`point_rt_iden`, `point_cl_rt_iden`, or `Outlet_Code`); then promote staging column if needed (`composite_rds_cluster_id` → `rds_cluster_id`).

### Hard-won lessons (must be preserved in automation)
- **Never match clusters by name alone.** "Dhanmondi" existed as 3 clusters under one point, one per route (`3a`, `3D`, `4D`). Scope by point **and** route (at minimum) before falling back to name.
- **Only patch missing rows.** Do not overwrite already-filled columns.
- **Kill `MAX(id)` collisions** — assign from a single snapshot (`ROW_NUMBER()` or token).
- Inconsistency to fix: File 1 uses the bare `point_id` without strip/lower while Files 3–5 normalize. Fine (100% match) but should be unified.
- Reconciliation: File 4 gap = 11,984 (route only) vs File 5 gap = 11,985 (point+route+cluster triplet). The extra row is an outlet whose route was unchanged but whose **cluster assignment changed** — a real data-change, not a bug. Cross-checks between `rds_route_id` (File 4) and `composite_rds_route_id` (File 5) must be surfaced, not silently merged.

---

## 2. Objective & Scope

Convert the current manual, prone-to-error 5-script + hand-SQL flow into a **reusable, PostgreSQL-backed automated pipeline** while keeping the familiar 5-script shape behind a shared module and a single orchestrator.

**In scope**
- Shared config + helpers module.
- Parameterized versions of the 5 scripts using the shared module.
- Automated detection + insert + backfill for new **routes, clusters, outlets** into `ecrm.locations`.
- PostgreSQL `INSERT ... RETURNING id` read-back of new ids.
- Reconciliation/validation across the route vs cluster stages.
- A `run_pipeline.py` orchestrator and a final verification gate.

**Out of scope / blocked (needs a decision before auto-running)**
- **New points**: the recipe defines no parent for a point level; point auto-insert is undefined. If any points appear unmatched, the run must **stop and alert** (manual review). (Historically 100% matched, so low risk.)
- DB credentials/DSN provisioning (to be supplied via env/config by the operator).
- Backfilling the upstream extract system (only `ecrm.locations` + the CSV are touched).

---

## 3. Target Architecture

```
location_migration/
  config.py                 # paths, DB DSN, table, type codes, normalization rules
  migration_common.py       # normalize/clean_cluster, build_lookup, apply_lookup,
                            #   connect(), insert_with_readback(), export_upload(),
                            #   find_cluster_id(), reconcile(), logging
  run_pipeline.py           # orchestrates stages 1,2, then ROUTE,CLUSTER,OUTLET, then verify

  # refactored scripts (preserve names; logic moved into migration_common)
  1.point_update_and_find.py          -> stage 1 (points - resolve only)
  2.update_3_identifier_column.py     -> stage 2 (build keys)
  3.update_rds_outlet_id.py           -> stage OUTLET (resolve + insert outlets)
  4.update_rds_route_id.py            -> stage ROUTE (resolve + insert routes)
  5.update_composite_cluster_route_id.py -> stage CLUSTER (resolve + insert clusters)

  data/                   # OLD_FILE, NEW_FILE, OUTPUT (config-specified, not in repo)
  finded_data/            # reports + new-location upload CSVs (kept as audit trail)
```

### Recommended execution order (top-down parent resolution)
Run stages as **1 → 2 → ROUTE → CLUSTER → OUTLET** (not the legacy 1→2→OUTLET→ROUTE→CLUSTER).
Rationale: a new row's `parent` must already be resolved. Routes parent to points, clusters to routes, outlets to clusters. Under the legacy order, a new outlet's cluster parent was hand-searched in the DB; by reordering, the outlet simply uses the now-resolved `rds_cluster_id` already on its own row — and it even works when the outlet's cluster/route are themselves new. The legacy scripts are only re-sequenced, not replaced; matching logic is unchanged and order-independent.

---

## 4. Key Design Decisions (confirmed)

| Decision | Choice |
|---|---|
| DB dialect | **PostgreSQL** (`psycopg2`), inserts use `INSERT ... RETURNING id` |
| Structure | Keep 5 scripts, add a shared `config.py` + `migration_common.py` + `run_pipeline.py` |
| New-id generation | **DB auto-increment + read-back** (`RETURNING id`) |
| Source locating | **Explicit paths** in `config.py` (`OLD_FILE`, `NEW_FILE`, `OUTPUT_FILE`) |
| New points | Alert & halt (out of scope) |
| Normalization | Unify on **strip + lowercase** for every join key; keep `clean_cluster` (dash→underscore) for cluster names; keep `point_cl_rt_iden` raw |

### Type codes (from observed upload files)
`route = 6`, `cluster = 7`, `outlet = 8`. Point type not used (no point inserts).

### `ecrm.locations` insert columns (from `new_route_upload.csv` / `new_cluster_upload.csv`)
`id, name, parent, type, is_deleted, created_at, updated_at, active, source_id`
Defaults: `is_deleted=FALSE`, `active=TRUE`, `created_at=updated_at=now()`. `id` is auto-generated by the sequence (omit from the INSERT list).

---

## 5. Implementation Task List (ordered)

### 5.1 `config.py`
- Config dataclass/constants: `OLD_FILE`, `NEW_FILE`, `OUTPUT_FILE`, `SAVE_DIR`, `TABLE`.
- DB via env/DSN: `DB_HOST/PORT/NAME/USER/PASSWORD` (or `DB_DSN`).
- Type-code map; column list; boolean/default literals.
- Runtime flags: `--dry-run`, `--stage`, `--write-db`, `--limit`.
- Mark `SOURCE_DIR` / placeholder for `data/` existence in `README` note.

### 5.2 `migration_common.py`
- `normalize(s)`: trim → strip internal? no—only trim + lower (join keys). Use `clean_cluster` only for cluster names.
- `clean_cluster(s)`: trim, remove all whitespace, `-`→`_`, lower.
- `build_lookup(old_df, key_col, val_col, normalize=None, cols=None)` →
  returns `dict` key→val; **asserts key uniqueness**; counts collisions and raises/logs if one key maps to >1 distinct `val`.
- `apply_lookup(df, key_col, val_col, lookup, normalize=None)` →
  maps **only** rows where `val_col` is null (golden rule); returns `(df, n_filled, n_missing)`; writes `missing_<val>.csv` when `n_missing>0`.
- `connect()` via psycopg2; `BEGIN`/`COMMIT` wrappers; `--dry-run` prints SQL instead.
- `insert_with_readback(conn, rows, type_code)` →
  inserts a batch and returns `source_id → new_id`. Use the **token-staging** technique to guarantee correct mapping:
  1. `CREATE TEMP TABLE _stg (token int, source_id text, name text, parent bigint, type int)` (on commit drop).
  2. Bulk insert staging rows with a sequential `token` (1..n).
  3. `INSERT INTO ecrm.locations (name,parent,type,is_deleted,active,created_at,updated_at,source_id) SELECT s.name,s.parent,s.type,FALSE,TRUE,now(),now(),s.source_id FROM _stg s RETURNING id, source_id`.
  4. Postgres `RETURNING` preserves processing order matching the SELECT order; fall back to matching `(source_id,parent,type,name)` if a collision is detected.
  5. Assert each `source_id` maps to exactly one id.
- `export_upload(rows, path)` for audit CSVs (`new_route_upload.csv`, `new_cluster_upload.csv`, etc.).
- `find_cluster_id(conn, point_id, route_name, cluster_name)` → DB hierarchy walk (point→route→cluster) with a **unique-match assertion**; never match on cluster name alone. (Retained for safety; primary path now uses `rds_cluster_id` from the reordered run.)
- `reconcile(df)`: assert `rds_route_id` (File 4) equals `composite_rds_route_id` (File 5) wherever both present; write `reconcile_warnings.csv` on mismatch. Logs direction.
- `log_summary(stage, filled, missing)`.

### 5.3 Stage scripts (refactor each to use the module)
1. **Points** (`1.*`): resolve `point_id → rds_point_id` (use `normalize` on both sides for consistency). Write `not_found_point.csv`. If any missing → print alert + **halt** (new points out of scope). Save output.
2. **Keys** (`2.*`): unchanged construction, but route all three key-builders through the helper; keep `point_cl_rt_iden` raw (no `<normalize>`), others normalized. Save output.
3. **ROUTE** (`4.*`): `build_lookup(route_section_id→rds_route_id)`; `apply_lookup`; on missing, collect distinct new `route_section_id`s; build insert rows `parent=rds_point_id, type=6, source_id=route_section_id`; `insert_with_readback`; export new-id map on `point_rt_iden`; backfill only missing `rds_route_id`.
4. **CLUSTER** (`5.*`): `build_lookup(point_cl_rt_iden→(rds_cluster_id,rds_route_id))`; `apply_lookup` into staging cols; on missing, dedupe by `(Cluster_Id, Cluster_Name, rds_route_id, point_cl_rt_iden)`; build insert rows `parent=rds_route_id, type=7, source_id=Cluster_Id`; `insert_with_readback`; export on `point_cl_rt_iden`; backfill only missing `composite_rds_cluster_id`, then promote to `rds_cluster_id`.
5. **OUTLET** (`3.*`): `build_lookup(Outlet_Code→rds_outlet_id)`; `apply_lookup`; on missing, build insert rows `parent=rds_cluster_id` (now resolved by the CLUSTER stage), `type=8, source_id=Outlet_Code`; `insert_with_readback`; backfill only missing `rds_outlet_id`.

### 5.4 `run_pipeline.py`
- Parse flags; load `config.csv` paths.
- Run stages in order `1 → 2 → ROUTE → CLUSTER → OUTLET → verify`.
- Honor `--dry-run` (no writes to DB/CSV), `--stage <name>`, `--skip-stage`, `--write-db`.
- Print a per-stage summary table.

### 5.5 `requirements.txt`/environment
- Add `pandas`, `psycopg2-binary`, `python-dotenv`; document `venv` activate + install.

### 5.6 Documentation update
- Refresh `location_migration_pipeline.md`: note the automated flow, the reordered stage sequence, and that `data/*.csv` is expected to be dropped into `data/`.

---

## 6. Automated Gap-Filling Recipe (top-down, set-based)

For each level (route → cluster → outlet), in one transaction:
1. Resolve existing rows (`apply_lookup`) → split into `matched` / `missing`.
2. Dedupe `missing` to the distinct parent+name per `source_id` (collision-check the key).
3. `insert_with_readback` → `source_id → new_id`.
4. CSV backfill **only** on `isna()` rows via the join key; promote staging column if used.
5. `export_upload` for the audit trail; move to next level.

---

## 7. Validation & Verification

- **Null check (gate):** after all stages, `df[['rds_point_id','rds_outlet_id','rds_route_id','rds_cluster_id']].isna().sum()` must be `0`.
- **Collision asserts:** each join key maps to exactly one id in the old file; each new `source_id` maps to exactly one new id.
- **Reconciliation:** `rds_route_id == composite_rds_route_id` wherever both present; mismatches written to `reconcile_warnings.csv` and logged.
- **Read-back determinism:** assert new-id mapping length == inserted row count; no duplicate new ids.
- **Dry-run parity:** run `--dry-run` first (expected counts logged, no writes), then real run, then re-run null gate.

---

## 8. Risks / Edge Cases / Open Items

- **New points** → halt + alert (parent chain undefined). Confirmed out of scope.
- **`source_id` collisions for clusters**: the same `Cluster_Id` can appear under different routes. The dedupe on `(Cluster_Id, Cluster_Name, rds_route_id, point_cl_rt_iden)` and read-back keyed on `(source_id, parent, type, name)` guard against this.
- **`INSERT ... RETURNING` order guarantee**: relies on token-staging; must be validated with a small test batch before a full run.
- **Reordered stages vs. legacy raw scripts**: the original `3/4/5` files are kept but their numbering no longer reflects execution order — comment clearly.
- **`data/` presence**; operator must supply `OLD_FILE`, `NEW_FILE`. `missing_outlet_id.csv` for the 2 hand-fixed outlets was never committed.
- **DB credentials** come from env config; not committed.
- **`.xlsx` reference files** are manual aids; no automation intended.

---

## 9. Handoff / Next Step

This plan is implementation-ready for an implementation-capable agent. The build order is 5.1 → 5.2 → 5.3 → 5.4 → 5.5 → 5.6, validating each stage in `--dry-run` before any DB/CSV writes. Switch to an implementation agent to execute it (requires the DB DSN, and dropping the current source extracts into `data/`).

**Open question for the operator before the first full auto-run:** the value of `type` for the point level (needed only if new points must ever be handled). If it stays 100% matched, the halt-and-alert behavior is sufficient.
