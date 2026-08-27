# Retailer Location Migration Pipeline

Migrates a fresh retailer extract (`retailers_05_07_2026.csv`) forward by carrying over RDS system identifiers — `rds_point_id`, `rds_outlet_id`, `rds_route_id`, `rds_cluster_id` — from the previous, already-mapped file (`retailers_02_05_2026_update.csv`). Five scripts run in sequence, each resolving one identifier by joining old and new data on the correct key. Rows that can't be resolved from the old file are genuinely new locations and require manual creation in `ecrm.locations`, followed by a backfill into the CSV.

## Pipeline at a glance

| Stage | Script | Resolves | Join key | Result |
|---|---|---|---|---|
| 1 | `1_point_update_and_find.py` | `rds_point_id` | `point_id` | 708,748 / 708,748 matched (100%) |
| 2 | `2_update_3_identifier_column.py` | `point_rt_iden`, `point_cl_rt_iden`, `point_cl_iden` | n/a — builds keys | all rows, pure construction |
| 3 | `3_update_rds_outlet_id.py` | `rds_outlet_id` | `Outlet_Code` (normalized) | 708,746 matched, 2 missing |
| 4 | `4_update_rds_route_id.py` | `rds_route_id` | `route_section_id` | 696,764 matched, 11,984 missing |
| 5 | `5_update_composite_cluster_route_id.py` | `rds_cluster_id` (+ route cross-check) | `point_cl_rt_iden` | 696,763 matched, 11,985 missing |

Files 1–2 needed no manual work — the point hierarchy was fully stable, and File 2 only builds keys. Files 3–5 each surfaced genuinely new locations that had to be created in `ecrm.locations` before the CSV could be fully resolved.

## File 1 — point mapping

Loads the new extract, adds seven empty placeholder columns, then builds a `point_id → rds_point_id` lookup from the old file (437 unique points) and left-merges it in. Unmatched points would go to `finded_data/not_found_point.csv`, deduplicated by `point_id`.

- Result: all 708,748 rows matched. No new points existed between the two extracts.
- Note: the join key is used raw here, without the strip/lower normalization used in later scripts — worked because the match rate was already 100%, but it's an inconsistency worth watching.

## File 2 — build identifier keys

Pure key construction, no matching against the old file.

| Column | Built from | Purpose |
|---|---|---|
| `point_rt_iden` | `rds_point_id + Point + Route/Sec` | point + route identity (RDS-side fields) |
| `point_cl_rt_iden` | `point_id + route_section_id + Cluster_Id` | point + route + cluster identity (raw ManushTech ids) — the strict key used in File 5 |
| `point_cl_iden` | `rds_point_id + Point + cleaned Cluster_Name` | point + cluster identity (RDS-side fields) |

`Cluster_Name` is normalized first (`clean_cluster`): trimmed, all internal whitespace stripped, dashes → underscores, lowercased — so `"Zone-A"` and `"zone a"` collapse to the same key.

## File 3 — outlet mapping

Builds `Outlet_Code → rds_outlet_id` from the old file, normalizing the key (strip + lowercase) on both sides. Two rows failed to match: `MT0601` (Foodi) and `MT0600` (The Cozy Bean), both new outlets under an existing point (MT Banani, `rds_point_id 2337`).

**Resolution:**
- Inserted new rows into `ecrm.locations` with `id = max(id)+1`, `parent` = the id of the correct existing **cluster**.
- Key lesson: **cluster name alone is not a safe match key.** "Dhanmondi" existed as three separate cluster rows under the same point, one per route (`3a`, `3D`, `4D`). The correct match required scoping by point **and** route, not point + cluster name alone.
- Verified: Foodi → cluster `1832971` (Badda, route `6a`); The Cozy Bean → cluster `1755568` (Dhanmondi, route `3a`).
- New outlet ids assigned: Foodi = `1830440`, The Cozy Bean = `1830441`.

**Backfill (2 rows, patched directly):**
```python
patch_map = {'MT0601': '1830440', 'MT0600': '1830441'}
mask = df['rds_outlet_id'].isna()
df.loc[mask, 'rds_outlet_id'] = df.loc[mask, 'Outlet_Code'].map(patch_map)
```

## File 4 — route mapping

Builds `route_section_id → rds_route_id` from the old file (10,238 unique routes). 11,984 rows (~1.7%) had no match — a much larger gap than points or outlets, consistent with routes being restructured more often than the point hierarchy.

**Insert-ready file for missing routes:**

| Column | Value | Purpose |
|---|---|---|
| `id` | `max(id)` in `ecrm.locations`, +1 per row | new route's own id |
| `name` | `Route/Sec` | route display name |
| `parent` | `rds_point_id` | already fully resolved in File 1 — no ambiguity |
| `type` | `6` | route-level row |
| `source_id` | `route_section_id` | original ManushTech id, kept for traceability |

**Backfill (11,984 rows, keyed on `point_rt_iden`):**
```python
new_routes = pd.read_csv('new_routes_inserted.csv', dtype=str)   # columns: id, point_rt_iden
lookup = new_routes.set_index('point_rt_iden')['id'].to_dict()

mask = df['rds_route_id'].isna()
df.loc[mask, 'rds_route_id'] = df.loc[mask, 'point_rt_iden'].map(lookup)
```
`point_rt_iden` is used rather than `source_id`/`route_section_id` directly because it's already present on every row of the main CSV (built in File 2) — avoids a second join.

## File 5 — composite cluster/route mapping

The only stage that resolves `rds_cluster_id`. Builds `point_cl_rt_iden → (rds_cluster_id, rds_route_id)` from the old file (132,776 unique composite keys). 11,985 rows failed to match — one more than File 4's route gap, because this key needs the full point + route + cluster triplet to exist in the old file, not just the route alone. The extra row is an outlet whose route was unchanged but whose **cluster assignment changed**.

**Insert-ready file for missing clusters (same shape, one level down):**

| Column | Value | Purpose |
|---|---|---|
| `id` | `max(id)` in `ecrm.locations`, +1 per row | new cluster's own id |
| `name` | `Cluster_Name` | cluster display name |
| `parent` | `rds_route_id` | already resolved in File 4 — no ambiguity |
| `type` | `7` | cluster-level row |
| `source_id` | `Cluster_Id` | original ManushTech id, kept for traceability |

Before building the insert file: deduplicated on `Cluster_Id, Cluster_Name, rds_route_id, point_cl_rt_iden`, and checked that no single `Cluster_Id` maps to more than one distinct `rds_route_id`.

**Backfill (11,985 rows, keyed on `point_cl_rt_iden`), then promoted to the real column:**
```python
new_clusters = pd.read_csv('new_clusters_inserted.csv', dtype=str)  # columns: id, point_cl_rt_iden
lookup = new_clusters.set_index('point_cl_rt_iden')['id'].to_dict()

mask = df['composite_rds_cluster_id'].isna()
df.loc[mask, 'composite_rds_cluster_id'] = df.loc[mask, 'point_cl_rt_iden'].map(lookup)

# promote the resolved staging column into the real column
df['rds_cluster_id'] = df['composite_rds_cluster_id']
```

## The recurring gap-filling recipe

Files 3, 4, and 5 each hit the same shape of problem, resolved with the same five-step recipe — only the hierarchy level changes:

1. **Identify missing rows** — no match against the old file for the relevant key.
2. **Build an insert-ready file** — new `id` (`max(id)+n`), `name` from the CSV, `parent` = the already-resolved id one level up, `source_id` preserving the original raw ManushTech id.
3. **Insert** those rows into `ecrm.locations`.
4. **Look up the new ids by `source_id`** — exact and unambiguous, unlike matching by name (see the Dhanmondi case).
5. **Backfill the main CSV** via the join key, touching only previously-missing rows, then promote to the final column if needed.

| Level | Parent resolved from | Ambiguity risk |
|---|---|---|
| Outlet (type 8) | Cluster — looked up by name, manually | High — cluster names repeat across routes under the same point; must scope by (point, route, cluster name) |
| Route (type 6) | `rds_point_id` — resolved by File 1 | None — point already unambiguous |
| Cluster (type 7) | `rds_route_id` — resolved by File 4 | None — route already unambiguous |

The outlet case needed more manual work because there was no `source_id`-style shortcut yet — the cluster had to be found by name, requiring the extra route filter. For bulk cases (many missing outlets at once), the same recipe should be a single set-based SQL join rather than one lookup per row, using `ROW_NUMBER()` over a single `MAX(id)` snapshot to assign new ids without collisions.

**Golden rule for every backfill:** only patch the rows that are still missing — never overwrite the whole column, since most rows were already correctly filled by the script itself.

## Final verification

After all five scripts plus the manual insertions and backfills, every row should have all four ids filled:
```python
df[['rds_point_id', 'rds_outlet_id', 'rds_route_id', 'rds_cluster_id']].isna().sum()
```
All four should return zero.

## Checklist for next time

- [ ] Run File 1 → check `finded_data/not_found_point.csv` for genuinely new points.
- [ ] Run File 2 → no output to check, just confirm it completes without error.
- [ ] Run File 3 → for any missing outlets, resolve cluster parent by (point, route, cluster name) — never by cluster name alone. Patch `rds_outlet_id` directly by `Outlet_Code` if the count is small.
- [ ] Run File 4 → for any missing routes, insert with `parent = rds_point_id` and `source_id = route_section_id`, export the new ids with `point_rt_iden`, fill only the previously-missing `rds_route_id` rows via that lookup.
- [ ] Run File 5 → for any missing clusters, insert with `parent = rds_route_id` and `source_id = Cluster_Id`, export the new ids with `point_cl_rt_iden`, fill only the previously-missing `composite_rds_cluster_id` rows, then copy into `rds_cluster_id`.
- [ ] Run the final `isna().sum()` check across all four id columns before signing off.

---

## Automated pipeline (new)

The manual flow is now wrapped by a reusable, PostgreSQL-backed pipeline. The
five stages were kept as their own entry points but share a common module, and a
single orchestrator runs them in **top-down order** so each new row's `parent`
is already resolved:

```
point -> keys -> route -> cluster -> outlet -> verify
```

### Files
| File | Purpose |
|---|---|
| `config.py` | Paths, DB DSN, type codes, normalization rules |
| `migration_common.py` | Shared: lookups, normalization, DB insert + `RETURNING id` read-back, stage functions, reconcile/verify |
| `run_pipeline.py` | Orchestrator. `--dry-run`, `--write-db`, `--stage`, `--skip-stage` |
| `1..5.*.py` | Thin wrappers for running a single stage on demand |
| `gui_pipeline.py` | A single-file GUI over the pipeline |
| `Install_Library.py` | Installer that pip-installs deps behind a progress bar |

### Run it
```bash
python run_pipeline.py --dry-run     # validate counts, no writes
python run_pipeline.py --write-db    # full run: insert new rows + backfill + verify
```

### Ordering note
Run route (`4`) and cluster (`5`) **before** outlet (`3`). Outlets are resolved
by `Outlet_Code`, but a new outlet's parent is its (now-resolved) cluster id, so
the cluster stage must have already run. New points remain out of scope — the
pipeline halts with an alert if any appear.

### Safety rules carried over
- Only patch rows that are still missing (`isna`) — never overwrite filled columns.
- Insert from a single staged batch with `MAX(id)`-free auto-increment + read-back.
- Never match clusters by name alone; scope by point/route first.
- Verify all four id columns are fully populated before signing off.
