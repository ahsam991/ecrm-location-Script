import os
import pandas as pd

# ============================================================
# CONFIG
# ============================================================
UPDATE_FILE = 'data/retailers_05_07_2026_update.csv'
OLD_FILE    = 'data/retailers_02_05_2026_update.csv'
NOT_FOUND_FILE = 'finded_data/missing_outlet_id.csv'

# ============================================================
# STEP 1: Load update file
# ============================================================
print("STEP 1: Loading update file...", flush=True)
df = pd.read_csv(UPDATE_FILE, dtype=str)
print(f"  -> Total rows: {len(df)}", flush=True)

# ============================================================
# STEP 2: Load outlet_code -> rds_outlet_id mapping from old file
# ============================================================
print("\nSTEP 2: Loading outlet_code -> rds_outlet_id mapping from old file...", flush=True)
df_old = pd.read_csv(
    OLD_FILE,
    usecols=['Outlet_Code', 'rds_outlet_id'],
    dtype=str
)

# Normalize: lower + trim, then drop duplicates
df_old['outlet_code_key'] = df_old['Outlet_Code'].str.strip().str.lower()
df_old = df_old.drop_duplicates(subset=['outlet_code_key']).dropna(subset=['outlet_code_key'])
print(f"  -> Unique outlet_code found in old file: {len(df_old)}", flush=True)

# Build lookup dict: normalized outlet_code -> rds_outlet_id
lookup = df_old.set_index('outlet_code_key')['rds_outlet_id'].to_dict()

# ============================================================
# STEP 3: Update existing rds_outlet_id column in place
# ============================================================
print("\nSTEP 3: Matching outlet_code and updating rds_outlet_id...", flush=True)

# Normalize key in update file
outlet_key = df['Outlet_Code'].str.strip().str.lower()

# Update rds_outlet_id using map (keep existing value if no match)
df['rds_outlet_id'] = outlet_key.map(lookup)

filled = df['rds_outlet_id'].notna().sum()
empty  = df['rds_outlet_id'].isna().sum()
print(f"  -> rds_outlet_id filled: {filled} rows", flush=True)
print(f"  -> rds_outlet_id empty : {empty} rows (no match in old file)", flush=True)

# ============================================================
# STEP 4: Save updated file
# ============================================================
print(f"\nSTEP 4: Saving updated file to {UPDATE_FILE}...", flush=True)
df.to_csv(UPDATE_FILE, index=False)
print("  -> Done!", flush=True)

# ============================================================
# STEP 5: Save missing_outlet_id.csv if any rds_outlet_id empty
# ============================================================
print("\nSTEP 5: Checking for missing rds_outlet_id...", flush=True)
df_missing = df[df['rds_outlet_id'].isna()].copy()

if len(df_missing) > 0:
    os.makedirs('finded_data', exist_ok=True)
    df_missing.to_csv(NOT_FOUND_FILE, index=False)
    print(f"  -> {len(df_missing)} row(s) missing rds_outlet_id.", flush=True)
    print(f"  -> Saved to: {NOT_FOUND_FILE}", flush=True)
else:
    print("  -> All rows have rds_outlet_id. No file created.", flush=True)

print("\nDone!", flush=True)
