import re
import pandas as pd

# ============================================================
# CONFIG
# ============================================================
INPUT_FILE  = 'data/retailers_05_07_2026_update.csv'
OUTPUT_FILE = 'data/retailers_05_07_2026_update.csv'   # overwrite same file

# ============================================================
# Helper: REGEXP_REPLACE equivalent
#   - Remove all whitespace  (\s+  -> '')
#   - Replace '-' with '_'
# ============================================================
def clean_cluster(val):
    if pd.isna(val):
        return ''
    val = str(val).strip()
    val = re.sub(r'\s+', '', val)   # remove all spaces
    val = val.replace('-', '_')     # dash -> underscore
    return val.lower()

# ============================================================
# STEP 1: Load file
# ============================================================
print("STEP 1: Loading input file...", flush=True)
df = pd.read_csv(INPUT_FILE, dtype=str)
print(f"  -> Total rows: {len(df)}", flush=True)

# ============================================================
# STEP 2: Build identifier columns
# ============================================================
print("\nSTEP 2: Building identifier columns...", flush=True)

# Shorthand series (stripped + lowered where needed)
rds_pt   = df['rds_point_id'].fillna('')
point    = df['Point'].str.strip().str.lower().fillna('')
route_sec= df['Route/Sec'].str.strip().str.lower().fillna('')
cluster  = df['Cluster_Name'].apply(clean_cluster) 

# manushtech id 
mt_point_id = df['point_id'].fillna('')
mt_route_sec_id = df['route_section_id'].fillna('')
mt_cluster_id = df['Cluster_Id'].fillna('')

# point_rt_iden  = '_' + rds_point_id + '_' + lower(trim(Point)) + '_' + lower(trim(Route/Sec))
df['point_rt_iden'] = '_' + rds_pt + '_' + point + '_' + route_sec

# point_cl_rt_iden = '_' + rds_point_id + '_' + lower(trim(Point)) + '_' + cluster_clean + '_' + lower(trim(Route/Sec))
df['point_cl_rt_iden'] = '_' + mt_point_id + '_' + mt_route_sec_id + '_' + mt_cluster_id

# point_cl_iden  = '_' + rds_point_id + '_' + lower(trim(Point)) + '_' + cluster_clean
df['point_cl_iden'] = '_' + rds_pt + '_' + point + '_' + cluster

print("  -> point_rt_iden    : done", flush=True)
print("  -> point_cl_rt_iden : done", flush=True)
print("  -> point_cl_iden    : done", flush=True)

# ============================================================
# STEP 3: Save output
# ============================================================
print(f"\nSTEP 3: Saving to {OUTPUT_FILE}...", flush=True)
df.to_csv(OUTPUT_FILE, index=False)

print("\nDone! File saved to:", OUTPUT_FILE, flush=True)
