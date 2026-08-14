import os
import pandas as pd

# ============================================================
# CONFIG - Change file names here if needed
# ============================================================
OLD_FILE    = 'data/retailers_02_05_2026_update.csv'
NEW_FILE    = 'data/retailers_05_07_2026.csv'
OUTPUT_FILE = 'data/retailers_05_07_2026_update.csv'

# New columns to add (from old file)
NEW_COLUMNS = [
    'rds_outlet_id',
    'point_cl_rt_iden',
    'point_cl_iden',
    'point_rt_iden',
    'rds_cluster_id',
    'rds_route_id',
    'rds_point_id',
]

# ============================================================
# STEP 1: Load new file and add empty columns
# ============================================================
print("STEP 1: Loading new file...", flush=True)
df_new = pd.read_csv(NEW_FILE, dtype=str)
print(f"  -> Total rows: {len(df_new)}", flush=True)

print("STEP 1: Adding new empty columns...", flush=True)
for col in NEW_COLUMNS:
    df_new[col] = None
print(f"  -> Columns added: {NEW_COLUMNS}", flush=True)

# ============================================================
# STEP 2: Load point_id -> rds_point_id mapping from old file
# ============================================================
print("\nSTEP 2: Loading point_id -> rds_point_id mapping from old file...", flush=True)
df_old_points = pd.read_csv(
    OLD_FILE,
    usecols=['point_id', 'rds_point_id'],
    dtype=str
)
df_old_points = df_old_points.drop_duplicates(subset=['point_id']).dropna(subset=['point_id'])
print(f"  -> Unique point_id found: {len(df_old_points)}", flush=True)

# ============================================================
# STEP 3: Match point_id and fill rds_point_id column
# ============================================================
print("\nSTEP 3: Matching point_id and filling rds_point_id...", flush=True)
df_new = df_new.drop(columns=['rds_point_id'])  # drop empty col before merge
df_new = pd.merge(df_new, df_old_points, on='point_id', how='left')

filled = df_new['rds_point_id'].notna().sum()
empty  = df_new['rds_point_id'].isna().sum()
print(f"  -> rds_point_id filled: {filled} rows", flush=True)
print(f"  -> rds_point_id empty : {empty} rows (no match in old file)", flush=True)

# ============================================================
# STEP 4: Save output file
# ============================================================
print(f"\nSTEP 4: Saving output to {OUTPUT_FILE}...", flush=True)
df_new.to_csv(OUTPUT_FILE, index=False)

print("\nDone! Output saved to:", OUTPUT_FILE, flush=True)

# ============================================================
# STEP 5: Save not_found_point.csv if any rds_point_id is empty
# ============================================================
print("\nSTEP 5: Checking for rows with empty rds_point_id...", flush=True)
NOT_FOUND_COLS = ['region_id', 'Region', 'area_id', 'Area', 'distributor_id', 'House', 'territory_id', 'Territory', 'point_id', 'Point']
df_not_found = df_new[df_new['rds_point_id'].isna()][NOT_FOUND_COLS].drop_duplicates(subset=['point_id'])

if len(df_not_found) > 0:
    NOT_FOUND_FILE = 'finded_data/not_found_point.csv'
    os.makedirs('finded_data', exist_ok=True)
    df_not_found.to_csv(NOT_FOUND_FILE, index=False)
    print(f"  -> {len(df_not_found)} unique point(s) not found in old file.", flush=True)
    print(f"  -> Saved to: {NOT_FOUND_FILE}", flush=True)
else:
    print("  -> All points have rds_point_id. No file created.", flush=True)
