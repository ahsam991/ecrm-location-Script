# Location Migration Pipeline — Quick Reference

**Print this page or bookmark it for quick lookup during development.**

---

## Project at a Glance

| Aspect | Details |
|--------|---------|
| **Purpose** | Migrate RDS identifiers (point, outlet, route, cluster) from old to new retailer dataset |
| **Input** | `retailers_02_05_2026_update.csv` (old) + `retailers_05_07_2026.csv` (new) |
| **Output** | `retailers_05_07_2026_update.csv` (all IDs filled) |
| **Stages** | 5 sequential Python scripts + 3 manual DB backfill steps |
| **Total Rows** | 708,748 locations |
| **Time** | ~4–6 hours first run; 30 min thereafter |

---

## Stage Checklist

```
□ Stage 1: python 1_point_update_and_find.py
  → Fills: rds_point_id
  → Gap file: not_found_point.csv
  → Expected: 100% match

□ Stage 2: python 2_update_3_identifier_column.py
  → Builds: point_rt_iden, point_cl_rt_iden, point_cl_iden
  → Gap file: (none; pure construction)
  → Expected: 100% built

□ Stage 3: python 3_update_rds_outlet_id.py
  → Fills: rds_outlet_id
  → Gap file: missing_outlet_id.csv
  → Expected: 99%+ match, small gaps patched manually

□ Stage 4: python 4_update_rds_route_id.py
  → Fills: rds_route_id
  → Gap file: missing_route_id.csv
  → Expected: 98%+ match, gaps → DB insert + backfill

□ Stage 5: python 5_update_composite_cluster_route_id.py
  → Fills: composite_rds_cluster_id, then rds_cluster_id
  → Gap file: missing_composite_cluster_route_id.csv
  → Expected: 98%+ match, gaps → DB insert + backfill

□ Verify: python verify.py
  → All 4 IDs should have 0 nulls
```

---

## Key Code Patterns

### Load & Prepare

```python
import pandas as pd
import re
import os

# Load with consistent type
df = pd.read_csv('file.csv', dtype=str)

# Add empty columns
for col in ['rds_point_id', 'rds_outlet_id']:
    df[col] = None

# Create output directory
os.makedirs('finded_data', exist_ok=True)
```

---

### Build Lookup Dictionary

```python
# From old file
df_old = pd.read_csv('old.csv', dtype=str)

# Normalize key
df_old['key_norm'] = df_old['key'].str.strip().str.lower()

# Deduplicate (CRITICAL)
df_old = df_old.drop_duplicates(subset=['key_norm']).dropna(subset=['key_norm'])

# Build lookup
lookup = df_old.set_index('key_norm')['id'].to_dict()
print(f"Lookup size: {len(lookup)}")
```

---

### Apply Lookup (Safe)

```python
# On new file
df_new['key_norm'] = df_new['key'].str.strip().str.lower()

# Method 1: Overwrite (for first fill)
df_new['id'] = df_new['key_norm'].map(lookup)

# Method 2: Preserve existing (for backfill)
mask = df_new['id'].isna()
df_new.loc[mask, 'id'] = df_new.loc[mask, 'key_norm'].map(lookup)

# Check results
filled = df_new['id'].notna().sum()
empty = df_new['id'].isna().sum()
print(f"Filled: {filled}, Empty: {empty}")
```

---

### Normalize Text

```python
# Simple normalization
def normalize(val):
    if pd.isna(val):
        return ''
    return val.strip().lower()

# Complex normalization
def clean_cluster(val):
    if pd.isna(val):
        return ''
    val = str(val).strip()
    val = re.sub(r'\s+', '', val)      # remove all spaces
    val = val.replace('-', '_')
    return val.lower()

# Apply
df['clean'] = df['messy'].apply(normalize)
```

---

### Build Composite Key

```python
# Concatenation (fastest)
df['key'] = '_' + df['col1'] + '_' + df['col2'] + '_' + df['col3']

# Using apply (most flexible)
def make_key(row):
    return f"_{row['col1']}_{row['col2']}_{row['col3']}"

df['key'] = df.apply(make_key, axis=1)

# Example
df['point_rt_iden'] = '_' + df['rds_point_id'] + '_' + df['Point'].str.lower() + '_' + df['Route/Sec'].str.lower()
```

---

### Handle Missing Rows

```python
# Find missing
df_missing = df[df['id'].isna()].copy()
print(f"Missing: {len(df_missing)}")

# Export gap file
os.makedirs('finded_data', exist_ok=True)
df_missing.to_csv('finded_data/missing_ids.csv', index=False)

# Conditional fill
mask = df['id'].isna()
df.loc[mask, 'id'] = df.loc[mask, 'key'].map(lookup)

# After fill, check again
still_missing = df['id'].isna().sum()
print(f"Still missing: {still_missing}")
```

---

### Save Output

```python
# Overwrite same file
df.to_csv('output.csv', index=False)

# Backup first
import shutil
shutil.copy('output.csv', 'output.csv.bak')
df.to_csv('output.csv', index=False)

# Verify written
df_check = pd.read_csv('output.csv')
print(f"Verified: {len(df_check)} rows")
```

---

## Common Debugging Commands

### Check Column Types

```python
df.dtypes
df['column'].dtype
```

### Sample Data

```python
df.head(10)
df.tail(5)
df[df['id'].isna()].head(3)  # see missing rows
```

### Value Counts

```python
df['column'].value_counts()
df['column'].value_counts(dropna=False)  # include NaN
```

### Find Duplicates

```python
df[df.duplicated(subset=['key'], keep=False)]
```

### Check for NaN

```python
df['column'].isna().sum()          # count NaNs
df.isnull().sum()                  # all columns
df[['col1', 'col2']].isna().sum()  # specific columns
```

### Memory & Performance

```python
df.info()  # dtypes + memory usage
df.memory_usage()  # per column
len(df)  # row count
```

---

## Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `KeyError: 'column_name'` | Column doesn't exist | Check spelling; `df.columns.tolist()` |
| `FileNotFoundError` | File path wrong | Use absolute path or check `pwd` |
| `NoneType` object has no attribute | Value is NULL | Use `.fillna()` or check with `.isna()` |
| Slow performance | Large dataset, inefficient join | Use `.map()` instead of `.merge()`; use `dtype=str` on load |
| Merge explosion | Many-to-many join | Use `drop_duplicates()` before merge |

---

## Decision Tree: Which Join Method?

```
Do you need to join on one column?
├─ Yes, and dataset fits in RAM?
│  └─ Use .map(lookup_dict)
│
└─ No, or multiple columns?
   ├─ Yes, need speed?
   │  └─ Use .map(lookup_dict) with composite key
   │
   └─ Need readability over speed?
      └─ Use .merge()
```

---

## File Structure

```
project/
├── data/
│   ├── retailers_02_05_2026_update.csv    (old, authoritative)
│   ├── retailers_05_07_2026.csv           (new extract)
│   └── retailers_05_07_2026_update.csv    (output, overwritten)
│
├── finded_data/
│   ├── not_found_point.csv                (from script 1)
│   ├── missing_outlet_id.csv              (from script 3)
│   ├── missing_route_id.csv               (from script 4)
│   └── missing_composite_cluster_route_id.csv (from script 5)
│
├── 1_point_update_and_find.py
├── 2_update_3_identifier_column.py
├── 3_update_rds_outlet_id.py
├── 4_update_rds_route_id.py
├── 5_update_composite_cluster_route_id.py
│
├── verify.py                              (run at end)
│
└── *.csv (insert files + backfill files, created manually)
    ├── new_route_upload.csv
    ├── new_cluster_upload.csv
    ├── new_routes_inserted.csv
    └── new_clusters_inserted.csv
```

---

## Final Verification

```python
import pandas as pd

df = pd.read_csv('data/retailers_05_07_2026_update.csv', dtype=str)

print("Row count:", len(df))
print("\nID column nulls:")
for col in ['rds_point_id', 'rds_outlet_id', 'rds_route_id', 'rds_cluster_id']:
    null_count = df[col].isna().sum()
    print(f"  {col}: {null_count:,}")

# All should be 0
assert df['rds_point_id'].isna().sum() == 0, "Missing rds_point_id!"
assert df['rds_outlet_id'].isna().sum() == 0, "Missing rds_outlet_id!"
assert df['rds_route_id'].isna().sum() == 0, "Missing rds_route_id!"
assert df['rds_cluster_id'].isna().sum() == 0, "Missing rds_cluster_id!"

print("\n✓ All checks passed!")
```

---

## Performance Tips

| Tip | Speedup |
|-----|---------|
| Use `dtype=str` on load | 2–3× |
| Use `.map()` instead of `.merge()` | 5–10× |
| Deduplicate before building lookup | 2× |
| Use `fillna()` instead of `.loc[]` | 1.5× |
| Use vectorized operations (not `.apply()`) | 10–100× |

---

## When to Ask for Help

✓ **Good questions:**
- "How do I match on two columns instead of one?"
- "My join has way more empty rows than expected — how do I debug?"
- "The script ran but the file wasn't updated — where do I look?"

✗ **Bad questions:**
- "How do I use Python?" (learn basics first)
- "Is my data correct?" (check `.head()`, `.info()`, `.describe()`)
- "Can you write the script for me?" (try first, ask specific questions)

---

## Useful References

| Topic | Link |
|-------|------|
| pandas `.merge()` | https://pandas.pydata.org/docs/reference/api/pandas.merge.html |
| pandas `.map()` | https://pandas.pydata.org/docs/reference/api/pandas.Series.map.html |
| String methods `.str` | https://pandas.pydata.org/docs/user_guide/text.html |
| Regular expressions | https://docs.python.org/3/library/re.html |
| Python `.format()` | https://docs.python.org/3/library/stdtypes.html#str.format |

---

## One-Liner Cheat Sheet

```python
# Load and preview
df = pd.read_csv('f.csv', dtype=str); df.head()

# Normalize
df['col'] = df['col'].str.strip().str.lower().str.replace(' ', '_')

# Lookup
lu = dict(zip(df_old['k'], df_old['v'])); df['v'] = df['k'].map(lu)

# Check gaps
df['id'].isna().sum()

# Deduplicate
df = df.drop_duplicates(subset=['key'])

# Export missing
df[df['id'].isna()].to_csv('missing.csv', index=False)

# Conditional update
df.loc[df['id'].isna(), 'id'] = df.loc[df['id'].isna(), 'key'].map(lu)

# Verify
print(df[['id1', 'id2', 'id3']].isna().sum())  # should all be 0
```

---

**Last Updated:** August 2026  
**Questions?** See README.md or LEARNING_GUIDE.md
