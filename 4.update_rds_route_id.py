import os
import pandas as pd

# ============================================================
# CONFIG
# ============================================================
UPDATE_FILE    = 'data/retailers_05_07_2026_update.csv'
OLD_FILE       = 'data/retailers_02_05_2026_update.csv'
NOT_FOUND_FILE = 'finded_data/missing_route_id.csv'

# ============================================================
# STEP 1: Load update file
# ============================================================
print("STEP 1: Loading update file...", flush=True)
df = pd.read_csv(UPDATE_FILE, dtype=str)
print(f"  -> Total rows: {len(df)}", flush=True)

# ============================================================
# STEP 2: Load route_section_id -> rds_route_id mapping from old file
# ============================================================
print("\nSTEP 2: Loading route_section_id -> rds_route_id mapping from old file...", flush=True)
df_old = pd.read_csv(
    OLD_FILE,
    usecols=['route_section_id', 'rds_route_id'],
    dtype=str
)
df_old = df_old.drop_duplicates(subset=['route_section_id']).dropna(subset=['route_section_id'])
print(f"  -> Unique route_section_id found in old file: {len(df_old)}", flush=True)

# Build lookup dict: route_section_id -> rds_route_id
lookup = df_old.set_index('route_section_id')['rds_route_id'].to_dict()

# ============================================================
# STEP 3: Update existing rds_route_id column in place
# ============================================================
print("\nSTEP 3: Matching route_section_id and updating rds_route_id...", flush=True)

df['rds_route_id'] = df['route_section_id'].map(lookup)

filled = df['rds_route_id'].notna().sum()
empty  = df['rds_route_id'].isna().sum()
print(f"  -> rds_route_id filled: {filled} rows", flush=True)
print(f"  -> rds_route_id empty : {empty} rows (no match in old file)", flush=True)

# ============================================================
# STEP 4: Save updated file
# ============================================================
print(f"\nSTEP 4: Saving updated file to {UPDATE_FILE}...", flush=True)
df.to_csv(UPDATE_FILE, index=False)
print("  -> Done!", flush=True)

# ============================================================
# STEP 5: Save missing_route_id.csv if any rds_route_id empty
# ============================================================
print("\nSTEP 5: Checking for missing rds_route_id...", flush=True)
df_missing = df[df['rds_route_id'].isna()].copy()

if len(df_missing) > 0:
    os.makedirs('finded_data', exist_ok=True)
    df_missing.to_csv(NOT_FOUND_FILE, index=False)
    print(f"  -> {len(df_missing)} row(s) missing rds_route_id.", flush=True)
    print(f"  -> Saved to: {NOT_FOUND_FILE}", flush=True)
else:
    print("  -> All rows have rds_route_id. No file created.", flush=True)

print("\nDone!", flush=True)
