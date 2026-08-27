"""Shared helpers for the Retailer Location Migration pipeline.

Every stage resolves one RDS system id by joining the fresh extract against the
previous, already-mapped extract. New (unmatched) locations at each level are
inserted into the destination table, and the extracted id is back-filled into
the running DataFrame.

Golden rule preserved throughout: only patch rows that are still missing.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

import pandas as pd

log = logging.getLogger("migration")


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def clean_cluster(val: Any) -> str:
    """Collapse cluster names for safe comparison.

    Trim, drop all internal whitespace, dash -> underscore, lowercase. This makes
    "Zone-A" and "zone a" resolve to the same key.
    """
    if pd.isna(val):
        return ""
    val = str(val).strip()
    val = re.sub(r"\s+", "", val)
    val = val.replace("-", "_")
    return val.lower()


def normalize(val: Any) -> Any:
    """Trim + lowercase for general join keys."""
    if pd.isna(val):
        return None
    val = str(val).strip().lower()
    return val if val else None


def strip_only(val: Any) -> Any:
    """Trim only (no case change) for keys already lowercased upstream."""
    if pd.isna(val):
        return None
    val = str(val).strip()
    return val if val else None


# --------------------------------------------------------------------------- #
# Runtime options
# --------------------------------------------------------------------------- #
@dataclass
class Opts:
    dry_run: bool = False
    write_db: bool = False
    write_csv: bool = True
    conn: Any = None
    save_dir: str = "finded_data"
    limit: int = 0
    stats: dict = field(default_factory=dict)

    def record(self, stage: str, filled: int, missing: int) -> None:
        self.stats[stage] = {"filled": int(filled), "missing": int(missing)}


# --------------------------------------------------------------------------- #
# DataFrame helpers
# --------------------------------------------------------------------------- #
def ensure_columns(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for col in cols:
        if col not in df.columns:
            df[col] = None
    return df


def report(df: pd.DataFrame, path: str) -> None:
    """Write a DataFrame to an audit/report CSV, creating parent dirs."""
    if df is None or len(df) == 0:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)
    log.info("  wrote %d rows -> %s", len(df), path)


# --------------------------------------------------------------------------- #
# Lookup construction / application
# --------------------------------------------------------------------------- #
def build_lookup(
    old_df: pd.DataFrame,
    key_col: str,
    val_col: str,
    normalize_fn: Optional[Callable[[Any], Any]] = None,
) -> dict:
    """Build a key -> val dict from the old extract.

    Logs a warning (but keeps the first value, matching the original behaviour)
    when one key maps to more than one distinct value, which indicates an
    ambiguous join.
    """
    if key_col not in old_df.columns or val_col not in old_df.columns:
        return {}
    tmp = old_df[[key_col, val_col]].copy()
    if normalize_fn:
        tmp["_k"] = tmp[key_col].map(normalize_fn)
    else:
        tmp["_k"] = tmp[key_col]
    tmp = tmp[tmp["_k"].notna() & (tmp["_k"].astype(str).str.strip() != "")].copy()
    tmp = tmp.dropna(subset=[val_col])
    if len(tmp) == 0:
        return {}

    distinct_vals = tmp.groupby("_k")[val_col].nunique()
    collisions = distinct_vals[distinct_vals > 1]
    if len(collisions) > 0:
        log.warning(
            "Ambiguous lookup on %s -> %s: %d key(s) map to >1 value. Using first. e.g. %s",
            key_col, val_col, len(collisions), list(collisions.index[:5]),
        )
    return tmp.drop_duplicates(subset=["_k"]).set_index("_k")[val_col].to_dict()


def apply_lookup(
    df: pd.DataFrame,
    key_col: str,
    col: str,
    lookup: Mapping[Any, Any],
    normalize_fn: Optional[Callable[[Any], Any]] = None,
) -> tuple[int, int]:
    """Fill `col` on rows that are still missing, using a key -> value lookup.

    Only rows where `col` is currently null are touched (golden rule). Returns
    (n_filled, n_missing) counts.
    """
    ensure_columns(df, [col])
    keys = df[key_col].map(normalize_fn) if normalize_fn else df[key_col]
    mapped = keys.map(lookup)
    mask = df[col].isna()
    df.loc[mask, col] = mapped[mask]
    filled = int(df[col].notna().sum())
    missing = int(df[col].isna().sum())
    return filled, missing


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def connect(cfg, dry_run: bool = False):
    """Open a PostgreSQL connection (autocommit off). None in dry-run."""
    if dry_run:
        return None
    try:
        import psycopg2  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "psycopg2 is not installed. Run `pip install -r requirements.txt`."
        ) from exc
    dsn = os.getenv("DB_DSN")
    if dsn:
        conn = psycopg2.connect(dsn)
    else:
        conn = psycopg2.connect(
            host=cfg.db_host or None,
            port=cfg.db_port or None,
            dbname=cfg.db_name or None,
            user=cfg.db_user or None,
            password=cfg.db_password or None,
        )
    conn.autocommit = False
    return conn


def insert_with_readback(
    conn,
    rows: list[dict],
    type_code: int,
    cfg,
    dry_run: bool = False,
) -> dict:
    """Insert new location rows and return {source_id: new_id}.

    Rows are {} with keys: source_id, name, parent, type.

    Uses a temporary staging table carrying a unique token per row, then a single
    INSERT ... SELECT ... RETURNING id, source_id. Because RETURNING includes
    `source_id`, the id mapping is exact regardless of processing order. A guard
    raises on duplicate source_ids or duplicate inserts.
    """
    rows = [r for r in rows if r]
    if not rows:
        return {}
    if dry_run:
        log.info("  [dry-run] would insert %d row(s) type=%s into %s",
                 len(rows), type_code, cfg.table)
        return {}

    import psycopg2
    import psycopg2.extras

    # Pre-check that none of these source ids already exist (prevents duplicates
    # on a partial re-run).
    srcs = [str(r["source_id"]) for r in rows if r.get("source_id") is not None]
    if len(srcs) != len(rows):
        conn.rollback()
        raise ValueError("Every new row must carry a non-null source_id.")
    cur = conn.cursor()
    cur.execute(
        f"SELECT count(*) FROM {cfg.table} WHERE type = %s AND source_id = ANY(%s)",
        (type_code, srcs),
    )
    if cur.fetchone()[0] > 0:
        conn.rollback()
        raise RuntimeError(
            f"Source ids already exist in {cfg.table} for type {type_code}. "
            "Aborting to avoid duplicate inserts; reconcile (reset sequence / re-run clean) first."
        )

    cur.execute(
        "CREATE TEMP TABLE _stg (token int, source_id text, name text, "
        "parent bigint, type int) ON COMMIT DROP"
    )
    args = []
    for i, r in enumerate(rows):
        name = "" if r.get("name") is None else str(r["name"])
        args.append((i + 1, str(r["source_id"]), name, int(r["parent"]), int(r["type"])))
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO _stg (token, source_id, name, parent, type) VALUES %s",
        args,
        page_size=1000,
    )
    cur.execute(
        f"""
        INSERT INTO {cfg.table} (name, parent, type, is_deleted, active,
                                 created_at, updated_at, source_id)
        SELECT s.name, s.parent, s.type, FALSE, TRUE, now(), now(), s.source_id
        FROM _stg s
        RETURNING id, source_id
        """
    )
    pairs = cur.fetchall()
    cur.close()

    mapping: dict = {}
    for new_id, source_id in pairs:
        if source_id in mapping:
            conn.rollback()
            raise RuntimeError(
                f"source_id {source_id} returned multiple ids ({mapping[source_id]}, {new_id})."
            )
        mapping[source_id] = new_id
    if len(mapping) != len(rows):
        conn.rollback()
        raise RuntimeError(f"Read-back returned {len(mapping)} ids for {len(rows)} rows.")
    log.info("  inserted %d row(s) type=%s; read back %d new id(s)",
             len(rows), type_code, len(mapping))
    return mapping


def insert_and_backfill(
    df: pd.DataFrame,
    missing_df: pd.DataFrame,
    cfg,
    opts: Opts,
    *,
    source_id_col: str,
    name_col: str,
    parent_col: str,
    id_col: str,
    level: str,
    dedup_cols: Iterable[str],
    upload_fname: str,
) -> int:
    """Shared gap-filling for a single level.

    Deduplicates the missing rows, inserts them into the DB (auto-increment +
    read-back), then back-fills `id_col` on every still-missing row via the
    source id -> new id map. Returns the number of rows inserted.
    """
    type_code = cfg.type_for_level(level)
    if missing_df is None or len(missing_df) == 0:
        return 0

    distinct = missing_df.drop_duplicates(subset=list(dedup_cols)).copy()
    distinct = distinct[distinct[source_id_col].notna()]
    if len(distinct) == 0:
        return 0

    if opts.dry_run:
        log.info("  [dry-run] %s: would insert %d distinct new row(s)",
                 level, len(distinct))
        return len(distinct)

    if not opts.write_db:
        log.info("  %s: skipped DB insert (--write-db not set); %d new row(s) pending. "
                 "Re-run with --write-db to insert them.",
                 level, len(distinct))
        return len(distinct)

    rows = []
    for _, r in distinct.iterrows():
        parent = r[parent_col]
        if pd.isna(parent):
            raise RuntimeError(
                f"New {level} {r[source_id_col]} has unresolved parent "
                f"'{parent_col}'. Resolve the parent level before {level}."
            )
        rows.append({
            "source_id": r[source_id_col],
            "name": r[name_col],
            "parent": int(parent),
            "type": type_code,
        })

    mapping = insert_with_readback(opts.conn, rows, type_code, cfg, dry_run=False)
    if mapping:
        mask = df[id_col].isna()
        df.loc[mask, id_col] = df.loc[mask, source_id_col].map(mapping)
        upload = pd.DataFrame(rows, columns=["source_id", "name", "parent", "type"])
        report(upload, os.path.join(opts.save_dir, upload_fname))
        map_df = df.loc[mask, [source_id_col, id_col]].drop_duplicates()
        map_df = map_df.rename(columns={source_id_col: "source_id", id_col: "new_id"})
        report(map_df, os.path.join(opts.save_dir, upload_fname.replace(".csv", "_backfill_map.csv")))
    return len(rows)


# --------------------------------------------------------------------------- #
# Per-level stages
# --------------------------------------------------------------------------- #
BASE_VALIDATION_COLS = (
    "region_id", "Region", "area_id", "Area", "distributor_id", "House",
    "territory_id", "Territory", "point_id", "Point",
)


def stage_points(df: pd.DataFrame, cfg, opts: Opts) -> pd.DataFrame:
    """Resolve rds_point_id by point_id. New points are out of scope -> halt."""
    ensure_columns(df, cfg.target_columns)
    try:
        old = pd.read_csv(cfg.old_path, usecols=["point_id", "rds_point_id"], dtype=str)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Old extract not found. Set a valid `old_file` in config (or config.json)."
        ) from exc
    old = old.dropna(subset=["point_id"])
    lookup = build_lookup(old, "point_id", "rds_point_id", normalize_fn=normalize)
    filled, missing = apply_lookup(df, "point_id", "rds_point_id", lookup, normalize_fn=normalize)
    opts.record("point", filled, missing)
    log.info("[point] rds_point_id filled=%d missing=%d", filled, missing)
    if missing > 0:
        not_found = df[df["rds_point_id"].isna()].copy()
        cols = [c for c in BASE_VALIDATION_COLS if c in not_found.columns]
        not_found = not_found.drop_duplicates(subset=["point_id"])
        if opts.write_csv:
            report(not_found[cols], os.path.join(opts.save_dir, "not_found_point.csv"))
        raise RuntimeError(
            f"{missing} row(s) reference a new point that has no parent mapping. "
            "New points are out of scope - stop and review manually."
        )
    return df


def stage_keys(df: pd.DataFrame, cfg, opts: Opts) -> pd.DataFrame:
    """Build the three iden columns (pure construction, no DB)."""
    rds_pt = df["rds_point_id"].fillna("")
    point = df["Point"].str.strip().str.lower().fillna("") if "Point" in df else ""
    route_sec = df["Route/Sec"].str.strip().str.lower().fillna("") if "Route/Sec" in df else ""
    cluster = df["Cluster_Name"].apply(clean_cluster) if "Cluster_Name" in df else ""
    mt_point_id = df["point_id"].fillna("") if "point_id" in df else ""
    mt_route_sec_id = df["route_section_id"].fillna("") if "route_section_id" in df else ""
    mt_cluster_id = df["Cluster_Id"].fillna("") if "Cluster_Id" in df else ""

    df["point_rt_iden"] = "_" + rds_pt + "_" + point + "_" + route_sec
    df["point_cl_rt_iden"] = "_" + mt_point_id + "_" + mt_route_sec_id + "_" + mt_cluster_id
    df["point_cl_iden"] = "_" + rds_pt + "_" + point + "_" + cluster
    opts.record("key", len(df), 0)
    log.info("[key] built point_rt_iden / point_cl_rt_iden / point_cl_iden for %d rows", len(df))
    return df


def stage_route(df: pd.DataFrame, cfg, opts: Opts) -> pd.DataFrame:
    """Resolve rds_route_id by route_section_id; insert new routes."""
    old = pd.read_csv(cfg.old_path, usecols=["route_section_id", "rds_route_id"], dtype=str)
    old = old.dropna(subset=["route_section_id"])
    lookup = build_lookup(old, "route_section_id", "rds_route_id")
    filled, missing = apply_lookup(df, "route_section_id", "rds_route_id", lookup)
    opts.record("route", filled, missing)
    log.info("[route] rds_route_id filled=%d missing=%d", filled, missing)
    if missing > 0:
        missing_df = df[df["rds_route_id"].isna()].copy()
        if opts.write_csv:
            report(missing_df, os.path.join(opts.save_dir, "missing_route_id.csv"))
        insert_and_backfill(
            df, missing_df, cfg, opts,
            source_id_col="route_section_id", name_col="Route/Sec",
            parent_col="rds_point_id", id_col="rds_route_id", level="route",
            dedup_cols=["route_section_id", "Route/Sec", "rds_point_id"],
            upload_fname="new_route_upload.csv",
        )
    return df


def stage_cluster(df: pd.DataFrame, cfg, opts: Opts) -> pd.DataFrame:
    """Resolve rds_cluster_id (and a route cross-check) by point_cl_rt_iden.

    New clusters are inserted with parent = the already-resolved rds_route_id,
    then the staging column is promoted into the real column.
    """
    ensure_columns(df, ["composite_rds_cluster_id", "composite_rds_route_id"])
    old = pd.read_csv(
        cfg.old_path,
        usecols=["point_cl_rt_iden", "rds_cluster_id", "rds_route_id"],
        dtype=str,
    )
    old = old[old["point_cl_rt_iden"].notna() & (old["point_cl_rt_iden"].str.strip() != "")]
    lookup_cluster = build_lookup(old, "point_cl_rt_iden", "rds_cluster_id")
    lookup_route = build_lookup(old, "point_cl_rt_iden", "rds_route_id")
    apply_lookup(df, "point_cl_rt_iden", "composite_rds_cluster_id", lookup_cluster, normalize_fn=strip_only)
    apply_lookup(df, "point_cl_rt_iden", "composite_rds_route_id", lookup_route, normalize_fn=strip_only)

    missing = int(df["composite_rds_cluster_id"].isna().sum())
    opts.record("cluster", len(df) - missing, missing)
    log.info("[cluster] composite_rds_cluster_id resolved=%d missing=%d",
             len(df) - missing, missing)
    if missing > 0:
        missing_df = df[df["composite_rds_cluster_id"].isna()].copy()
        report_cols = [c for c in (
            "region_id", "Region", "area_id", "Area", "distributor_id", "House",
            "territory_id", "Territory", "point_id", "Point", "rds_point_id",
            "Cluster_Id", "Cluster_Name", "rds_cluster_id", "route_section_id",
            "rds_route_id", "point_cl_rt_iden", "composite_rds_cluster_id",
            "composite_rds_route_id",
        ) if c in missing_df.columns]
        if opts.write_csv:
            report(missing_df[report_cols], os.path.join(opts.save_dir, "missing_composite_cluster_route_id.csv"))

        # Guard: one Cluster_Id must never map to more than one rds_route_id.
        distinct = missing_df.drop_duplicates(subset=["Cluster_Id", "Cluster_Name", "rds_route_id", "point_cl_rt_iden"])
        per = distinct.groupby("Cluster_Id")["rds_route_id"].nunique()
        if (per > 1).any():
            raise RuntimeError(
                "A Cluster_Id maps to multiple rds_route_id; cannot insert safely. Review."
            )
        insert_and_backfill(
            df, missing_df, cfg, opts,
            source_id_col="Cluster_Id", name_col="Cluster_Name",
            parent_col="rds_route_id", id_col="composite_rds_cluster_id", level="cluster",
            dedup_cols=["Cluster_Id", "Cluster_Name", "rds_route_id", "point_cl_rt_iden"],
            upload_fname="new_cluster_upload.csv",
        )
    # Promote staging column into the real column.
    df["rds_cluster_id"] = df["composite_rds_cluster_id"]
    return df


def stage_outlet(df: pd.DataFrame, cfg, opts: Opts) -> pd.DataFrame:
    """Resolve rds_outlet_id by Outlet_Code; insert new outlets (parent = cluster)."""
    old = pd.read_csv(cfg.old_path, usecols=["Outlet_Code", "rds_outlet_id"], dtype=str)
    lookup = build_lookup(old, "Outlet_Code", "rds_outlet_id", normalize_fn=normalize)
    filled, missing = apply_lookup(df, "Outlet_Code", "rds_outlet_id", lookup, normalize_fn=normalize)
    opts.record("outlet", filled, missing)
    log.info("[outlet] rds_outlet_id filled=%d missing=%d", filled, missing)
    if missing > 0:
        missing_df = df[df["rds_outlet_id"].isna()].copy()
        if opts.write_csv:
            report(missing_df, os.path.join(opts.save_dir, "missing_outlet_id.csv"))
        insert_and_backfill(
            df, missing_df, cfg, opts,
            source_id_col="Outlet_Code", name_col="Outlet_Name",
            parent_col="rds_cluster_id", id_col="rds_outlet_id", level="outlet",
            dedup_cols=["Outlet_Code", "rds_cluster_id"],
            upload_fname="new_outlet_upload.csv",
        )
    return df


# --------------------------------------------------------------------------- #
# Reconciliation & verification
# --------------------------------------------------------------------------- #
REQUIRED_SOURCE_COLS = (
    "point_id", "Point", "route_section_id", "Route/Sec", "Cluster_Id",
    "Cluster_Name", "Outlet_Code", "Outlet_Name",
)


def validate_inputs(df: pd.DataFrame, cfg, opts: Opts) -> None:
    """Pre-flight validation (hand-note sec 6/10/18).

    Confirms the extract carries the required source columns, that the old
    /master file exists, and that outlet codes are not duplicated. Raises a
    clear error instead of silently continuing on incomplete input.
    """
    missing_cols = [c for c in REQUIRED_SOURCE_COLS if c not in df.columns]
    if missing_cols:
        raise RuntimeError("New extract is missing required columns: " + ", ".join(missing_cols))
    if not os.path.exists(cfg.old_path):
        raise RuntimeError(f"Old/master extract not found: {cfg.old_path}")
    if "Outlet_Code" in df.columns:
        codes = df["Outlet_Code"].dropna()
        dup = codes.duplicated().sum()
        if dup:
            raise RuntimeError(f"Found {int(dup)} duplicate Outlet_Code value(s) in the new extract.")
    log.info("[validate] columns present; outlet codes unique; old file found.")


def reconcile(df: pd.DataFrame, opts: Opts) -> int:
    """Assert route consistency between the route stage and cluster cross-check."""
    have = df["rds_route_id"].notna() & df["composite_rds_route_id"].notna()
    mismatch = have & (df["rds_route_id"] != df["composite_rds_route_id"])
    n = int(mismatch.sum())
    if n > 0:
        log.warning("Reconcile: %d row(s) where rds_route_id != composite_rds_route_id", n)
        if opts.write_csv:
            report(df[mismatch], os.path.join(opts.save_dir, "reconcile_warnings.csv"))
    else:
        log.info("Reconcile: rds_route_id matches composite_rds_route_id everywhere.")
    return n


def verify(df: pd.DataFrame, opts: Opts) -> dict:
    """Final gate: all four target id columns must be fully populated."""
    cols = ["rds_point_id", "rds_outlet_id", "rds_route_id", "rds_cluster_id"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    counts = df[cols].isna().sum()
    log.info("VERIFY isna().sum(): %s", counts.to_dict())
    n = int(counts.sum())
    if n > 0:
        raise RuntimeError(
            f"Verification FAILED: {n} row(s) still missing an id "
            f"({counts.to_dict()})."
        )
    return counts.to_dict()


def print_summary(opts: Opts) -> None:
    if not opts.stats:
        return
    print("\nStage summary")
    print(f"{'stage':<10}{'filled':>12}{'missing':>12}")
    for stage, s in opts.stats.items():
        print(f"{stage:<10}{s['filled']:>12}{s['missing']:>12}")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def add_common_args(parser) -> None:
    parser.add_argument("--config", default=None, help="Path to a config.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report only; no DB/CSV writes.")
    parser.add_argument("--write-db", action="store_true",
                        help="Insert new locations into the DB.")
    parser.add_argument("--write-csv", action="store_true", default=True,
                        help="Write the output/report CSVs (default on).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Optional cap on rows processed (for testing).")


def build_opts(cfg, args) -> Opts:
    dry_run = bool(args.dry_run)
    write_db = bool(args.write_db) and not dry_run
    return Opts(
        dry_run=dry_run,
        write_db=write_db,
        write_csv=bool(args.write_csv) and not dry_run,
        conn=None,
        save_dir=cfg.save_path,
        limit=int(args.limit),
    )
