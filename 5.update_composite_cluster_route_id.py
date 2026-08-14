import os
import pandas as pd

# ============================================================
# CONFIG
# ============================================================
UPDATE_FILE    = 'data/retailers_05_07_2026_update.csv'
OLD_FILE       = 'data/retailers_02_05_2026_update.csv'
NOT_FOUND_FILE = 'finded_data/missing_composite_cluster_route_id.csv'

# ============================================================
# STEP 1: Load update file
# ============================================================
print("STEP 1: Loading update file...", flush=True)
df = pd.read_csv(UPDATE_FILE, dtype=str)
print(f"  -> Total rows: {len(df)}", flush=True)

# ============================================================
# STEP 2: Load point_cl_rt_iden -> rds_cluster_id, rds_route_id
#         mapping from old file
# ============================================================
print("\nSTEP 2: Loading point_cl_rt_iden -> rds_cluster_id, rds_route_id mapping from old file...", flush=True)
df_old = pd.read_csv(
    OLD_FILE,
    usecols=['point_cl_rt_iden', 'rds_cluster_id', 'rds_route_id'],
    dtype=str
)

# Drop rows where key is null or empty
df_old = df_old[df_old['point_cl_rt_iden'].notna()]
df_old = df_old[df_old['point_cl_rt_iden'].str.strip() != '']

# Drop duplicates on key column (keep first)
df_old = df_old.drop_duplicates(subset=['point_cl_rt_iden'])

print(f"  -> Unique point_cl_rt_iden found in old file: {len(df_old)}", flush=True)

# Build lookup dicts
lookup_cluster = df_old.set_index('point_cl_rt_iden')['rds_cluster_id'].to_dict()
lookup_route   = df_old.set_index('point_cl_rt_iden')['rds_route_id'].to_dict()

# ============================================================
# STEP 3: Map composite_rds_cluster_id and composite_rds_route_id
#         using point_cl_rt_iden as the match key
# ============================================================
print("\nSTEP 3: Matching point_cl_rt_iden and filling composite IDs...", flush=True)

# Normalize key in update file (strip only — already lowercased from script 2)
key = df['point_cl_rt_iden'].str.strip()

df['composite_rds_cluster_id'] = key.map(lookup_cluster)
df['composite_rds_route_id']   = key.map(lookup_route)

filled_cluster = df['composite_rds_cluster_id'].notna().sum()
empty_cluster  = df['composite_rds_cluster_id'].isna().sum()
filled_route   = df['composite_rds_route_id'].notna().sum()
empty_route    = df['composite_rds_route_id'].isna().sum()

print(f"  -> composite_rds_cluster_id filled : {filled_cluster} rows", flush=True)
print(f"  -> composite_rds_cluster_id empty  : {empty_cluster} rows (no match)", flush=True)
print(f"  -> composite_rds_route_id   filled : {filled_route} rows", flush=True)
print(f"  -> composite_rds_route_id   empty  : {empty_route} rows (no match)", flush=True)

# ============================================================
# STEP 4: Save updated file
# ============================================================
print(f"\nSTEP 4: Saving updated file to {UPDATE_FILE}...", flush=True)
df.to_csv(UPDATE_FILE, index=False)
print("  -> Done!", flush=True)

# ============================================================
# STEP 5: Save missing rows (where both composite IDs are empty)
# ============================================================
print("\nSTEP 5: Checking for rows with missing composite IDs...", flush=True)
df_missing = df[
    df['composite_rds_cluster_id'].isna() & df['composite_rds_route_id'].isna()
].copy()

if len(df_missing) > 0:
    os.makedirs('finded_data', exist_ok=True)
    REPORT_COLS = [
        'region_id', 'Region', 'area_id', 'Area',
        'distributor_id', 'House', 'territory_id', 'Territory',
        'point_id', 'Point', 'rds_point_id',
        'Cluster_Id', 'Cluster_Name', 'rds_cluster_id',
        'route_section_id', 'rds_route_id',
        'point_cl_rt_iden',
        'composite_rds_cluster_id', 'composite_rds_route_id'
    ]
    # only keep cols that actually exist in df
    REPORT_COLS = [c for c in REPORT_COLS if c in df_missing.columns]
    df_missing[REPORT_COLS].to_csv(NOT_FOUND_FILE, index=False)
    print(f"  -> {len(df_missing)} row(s) have both composite IDs empty.", flush=True)
    print(f"  -> Saved to: {NOT_FOUND_FILE}", flush=True)
else:
    print("  -> All rows have at least one composite ID. No file created.", flush=True)

print("\nDone!", flush=True)
