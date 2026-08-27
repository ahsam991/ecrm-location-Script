"""Location_Automation_full - single-file glass UI for the Retailer Location
Migration pipeline.

This file is fully self-contained. It bundles the Config, all migration
helpers, and the PySide6 glass GUI. Run it directly:

    python Location_Automation_full.py

It checks for pandas, psycopg2, and PySide6 at startup; if any are missing it
asks (Tk prompt) before installing them with a progress bar.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REQUIRED = [("pandas", "pandas"), ("psycopg2", "psycopg2-binary"), ("PySide6", "PySide6")]
STAGE_ORDER = ["point", "key", "route", "cluster", "outlet"]
STAGE_SHORT = {"point": "Point", "key": "Keys", "route": "Route",
               "cluster": "Cluster", "outlet": "Outlet"}

log = logging.getLogger("migration")

# PS: pandas, psycopg2, PySide6 are imported lazily inside functions so the
# dependency gate (ensure_dependencies) runs before any import error.

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "config.json"


@dataclass
class Config:
    old_file: str = "data/retailers_02_05_2026_update.csv"
    new_file: str = "data/retailers_05_07_2026.csv"
    output_file: str = "data/retailers_05_07_2026_update.csv"
    save_dir: str = "finded_data"
    table: str = "ecrm.locations"
    point_type: int = 5
    route_type: int = 6
    cluster_type: int = 7
    outlet_type: int = 8
    db_host: str = ""
    db_port: int = 5432
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    target_columns: tuple = (
        "rds_outlet_id", "point_cl_rt_iden", "point_cl_iden", "point_rt_iden",
        "rds_cluster_id", "rds_route_id", "rds_point_id",
    )

    def resolve(self, path: str) -> str:
        p = Path(path)
        return str(p if p.is_absolute() else BASE_DIR / p)

    @property
    def old_path(self) -> str:
        return self.resolve(self.old_file)

    @property
    def new_path(self) -> str:
        return self.resolve(self.new_file)

    @property
    def output_path(self) -> str:
        return self.resolve(self.output_file)

    @property
    def save_path(self) -> str:
        return self.resolve(self.save_dir)

    def type_for_level(self, level: str) -> int:
        return {"point": self.point_type, "route": self.route_type,
                "cluster": self.cluster_type, "outlet": self.outlet_type}[level]


def load_config(path: str | None = None) -> Config:
    cfg = Config()
    cfg_path = Path(path) if path else CONFIG_FILE
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            known = set(asdict(Config()).keys())
            filtered = {k: v for k, v in data.items() if k in known}
            for k, v in filtered.items():
                if k == "target_columns":
                    v = tuple(v)
                setattr(cfg, k, v)
        except Exception:
            pass
    cfg.db_host = os.getenv("DB_HOST", cfg.db_host)
    cfg.db_port = int(os.getenv("DB_PORT", cfg.db_port))
    cfg.db_name = os.getenv("DB_NAME", cfg.db_name)
    cfg.db_user = os.getenv("DB_USER", cfg.db_user)
    cfg.db_password = os.getenv("DB_PASSWORD", cfg.db_password)
    return cfg


# --------------------------------------------------------------------------- #
# Migration helpers
# --------------------------------------------------------------------------- #

# ---- Normalization ----
def clean_cluster(val: Any) -> str:
    if val is None or (isinstance(val, float) and val != val):
        return ""
    val = str(val).strip()
    val = re.sub(r"\s+", "", val)
    val = val.replace("-", "_")
    return val.lower()


def normalize(val: Any) -> Any:
    if val is None or (isinstance(val, float) and val != val):
        return None
    val = str(val).strip().lower()
    return val if val else None


def strip_only(val: Any) -> Any:
    if val is None or (isinstance(val, float) and val != val):
        return None
    val = str(val).strip()
    return val if val else None


# ---- Runtime options ----
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


# ---- DataFrame helpers (lazy pandas) ----
def _ensure_columns(df, cols: Iterable[str]):
    for col in cols:
        if col not in df.columns:
            df[col] = None


def _report(df, path: str) -> None:
    if df is None or len(df) == 0:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)
    log.info("  wrote %d rows -> %s", len(df), path)


# ---- Lookup ----
def build_lookup(old_df, key_col: str, val_col: str,
                 normalize_fn: Optional[Callable[[Any], Any]] = None) -> dict:
    import pandas as pd
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
    collisions = tmp.groupby("_k")[val_col].nunique()
    coll = collisions[collisions > 1]
    if len(coll) > 0:
        log.warning("Ambiguous lookup %s->%s: %d key(s) map to >1 value. Using first.",
                    key_col, val_col, len(coll))
    return tmp.drop_duplicates(subset=["_k"]).set_index("_k")[val_col].to_dict()


def apply_lookup(df, key_col: str, col: str, lookup: Mapping[Any, Any],
                 normalize_fn: Optional[Callable[[Any], Any]] = None) -> tuple[int, int]:
    _ensure_columns(df, [col])
    keys = df[key_col].map(normalize_fn) if normalize_fn else df[key_col]
    mapped = keys.map(lookup)
    mask = df[col].isna()
    df.loc[mask, col] = mapped[mask]
    return int(df[col].notna().sum()), int(df[col].isna().sum())


# ---- Database (lazy psycopg2) ----
def connect(cfg, dry_run: bool = False):
    if dry_run:
        return None
    import psycopg2
    dsn = os.getenv("DB_DSN")
    if dsn:
        conn = psycopg2.connect(dsn)
    else:
        conn = psycopg2.connect(host=cfg.db_host or None, port=cfg.db_port or None,
                                dbname=cfg.db_name or None, user=cfg.db_user or None,
                                password=cfg.db_password or None)
    conn.autocommit = False
    return conn


def insert_with_readback(conn, rows: list[dict], type_code: int, cfg, dry_run: bool = False) -> dict:
    import psycopg2
    import psycopg2.extras
    rows = [r for r in rows if r]
    if not rows:
        return {}
    if dry_run:
        log.info("  [dry-run] would insert %d row(s) type=%s into %s", len(rows), type_code, cfg.table)
        return {}
    srcs = [str(r["source_id"]) for r in rows if r.get("source_id") is not None]
    if len(srcs) != len(rows):
        conn.rollback()
        raise ValueError("Every new row must carry a non-null source_id.")
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) FROM {cfg.table} WHERE type = %s AND source_id = ANY(%s)",
                (type_code, srcs))
    if cur.fetchone()[0] > 0:
        conn.rollback()
        raise RuntimeError(f"Source ids already exist in {cfg.table} for type {type_code}. Aborting.")
    cur.execute("CREATE TEMP TABLE _stg (token int, source_id text, name text, "
                "parent bigint, type int) ON COMMIT DROP")
    args = [(i + 1, str(r["source_id"]), str(r["name"]) if r.get("name") is not None else "",
             int(r["parent"]), int(r["type"])) for i, r in enumerate(rows)]
    psycopg2.extras.execute_values(cur, "INSERT INTO _stg (token,source_id,name,parent,type) VALUES %s",
                                   args, page_size=1000)
    cur.execute(f"INSERT INTO {cfg.table} (name,parent,type,is_deleted,active,created_at,updated_at,source_id) "
                f"SELECT s.name,s.parent,s.type,FALSE,TRUE,now(),now(),s.source_id FROM _stg s "
                f"RETURNING id, source_id")
    pairs = cur.fetchall()
    cur.close()
    mapping = {}
    for new_id, source_id in pairs:
        if source_id in mapping:
            conn.rollback()
            raise RuntimeError(f"source_id {source_id} returned multiple ids ({mapping[source_id]}, {new_id}).")
        mapping[source_id] = new_id
    if len(mapping) != len(rows):
        conn.rollback()
        raise RuntimeError(f"Read-back returned {len(mapping)} ids for {len(rows)} rows.")
    log.info("  inserted %d row(s) type=%s; read back %d new id(s)", len(rows), type_code, len(mapping))
    return mapping


def insert_and_backfill(df, missing_df, cfg, opts: Opts, *, source_id_col, name_col,
                        parent_col, id_col, level, dedup_cols, upload_fname) -> int:
    import pandas as pd
    type_code = cfg.type_for_level(level)
    if missing_df is None or len(missing_df) == 0:
        return 0
    distinct = missing_df.drop_duplicates(subset=list(dedup_cols)).copy()
    distinct = distinct[distinct[source_id_col].notna()]
    if len(distinct) == 0:
        return 0
    if opts.dry_run:
        log.info("  [dry-run] %s: would insert %d distinct new row(s)", level, len(distinct))
        return len(distinct)
    if not opts.write_db:
        log.info("  %s: skipped DB insert (--write-db not set); %d new row(s) pending.", level, len(distinct))
        return len(distinct)
    rows = []
    for _, r in distinct.iterrows():
        parent = r[parent_col]
        if parent is None or (isinstance(parent, float) and parent != parent):
            raise RuntimeError(f"New {level} {r[source_id_col]} has unresolved parent '{parent_col}'.")
        rows.append({"source_id": r[source_id_col], "name": r[name_col], "parent": int(parent), "type": type_code})
    mapping = insert_with_readback(opts.conn, rows, type_code, cfg)
    if mapping:
        mask = df[id_col].isna()
        df.loc[mask, id_col] = df.loc[mask, source_id_col].map(mapping)
        upload = pd.DataFrame(rows, columns=["source_id", "name", "parent", "type"])
        _report(upload, os.path.join(opts.save_dir, upload_fname))
        map_df = df.loc[mask, [source_id_col, id_col]].drop_duplicates()
        map_df = map_df.rename(columns={source_id_col: "source_id", id_col: "new_id"})
        _report(map_df, os.path.join(opts.save_dir, upload_fname.replace(".csv", "_backfill_map.csv")))
    return len(rows)


# ---- Input validation ----
BASE_VALID_COLS = ("region_id", "Region", "area_id", "Area", "distributor_id", "House",
                   "territory_id", "Territory", "point_id", "Point")
REQUIRED_SOURCE_COLS = ("point_id", "Point", "route_section_id", "Route/Sec",
                        "Cluster_Id", "Cluster_Name", "Outlet_Code", "Outlet_Name")


def validate_inputs(df, cfg, opts: Opts) -> None:
    missing_cols = [c for c in REQUIRED_SOURCE_COLS if c not in df.columns]
    if missing_cols:
        raise RuntimeError("New extract missing required columns: " + ", ".join(missing_cols))
    if not os.path.exists(cfg.old_path):
        raise RuntimeError(f"Old/master extract not found: {cfg.old_path}")
    import pandas as pd
    if "Outlet_Code" in df.columns:
        codes = df["Outlet_Code"].dropna()
        dup = codes.duplicated().sum()
        if dup:
            raise RuntimeError(f"Found {int(dup)} duplicate Outlet_Code value(s) in the new extract.")
    log.info("[validate] columns OK; outlet codes unique; old file found.")


# ---- Stage functions ----
def stage_points(df, cfg, opts: Opts):
    import pandas as pd
    _ensure_columns(df, cfg.target_columns)
    old = pd.read_csv(cfg.old_path, usecols=["point_id", "rds_point_id"], dtype=str)
    old = old.dropna(subset=["point_id"])
    lookup = build_lookup(old, "point_id", "rds_point_id", normalize_fn=normalize)
    filled, missing = apply_lookup(df, "point_id", "rds_point_id", lookup, normalize_fn=normalize)
    opts.record("point", filled, missing)
    if missing:
        not_found = df[df["rds_point_id"].isna()].copy()
        cols = [c for c in BASE_VALID_COLS if c in not_found.columns]
        not_found = not_found.drop_duplicates(subset=["point_id"])
        if opts.write_csv:
            _report(not_found[cols], os.path.join(opts.save_dir, "not_found_point.csv"))
        raise RuntimeError(f"{missing} row(s) reference a new point. New points are out of scope.")
    return df


def stage_keys(df, cfg, opts: Opts):
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
    return df


def stage_route(df, cfg, opts: Opts):
    import pandas as pd
    old = pd.read_csv(cfg.old_path, usecols=["route_section_id", "rds_route_id"], dtype=str)
    old = old.dropna(subset=["route_section_id"])
    lookup = build_lookup(old, "route_section_id", "rds_route_id")
    filled, missing = apply_lookup(df, "route_section_id", "rds_route_id", lookup)
    opts.record("route", filled, missing)
    if missing:
        missing_df = df[df["rds_route_id"].isna()].copy()
        if opts.write_csv:
            _report(missing_df, os.path.join(opts.save_dir, "missing_route_id.csv"))
        insert_and_backfill(df, missing_df, cfg, opts, source_id_col="route_section_id",
                            name_col="Route/Sec", parent_col="rds_point_id", id_col="rds_route_id",
                            level="route", dedup_cols=["route_section_id", "Route/Sec", "rds_point_id"],
                            upload_fname="new_route_upload.csv")
    return df


def stage_cluster(df, cfg, opts: Opts):
    import pandas as pd
    _ensure_columns(df, ["composite_rds_cluster_id", "composite_rds_route_id"])
    old = pd.read_csv(cfg.old_path, usecols=["point_cl_rt_iden", "rds_cluster_id", "rds_route_id"], dtype=str)
    old = old[old["point_cl_rt_iden"].notna() & (old["point_cl_rt_iden"].str.strip() != "")]
    lookup_cluster = build_lookup(old, "point_cl_rt_iden", "rds_cluster_id")
    lookup_route = build_lookup(old, "point_cl_rt_iden", "rds_route_id")
    apply_lookup(df, "point_cl_rt_iden", "composite_rds_cluster_id", lookup_cluster, normalize_fn=strip_only)
    apply_lookup(df, "point_cl_rt_iden", "composite_rds_route_id", lookup_route, normalize_fn=strip_only)
    missing = int(df["composite_rds_cluster_id"].isna().sum())
    opts.record("cluster", len(df) - missing, missing)
    if missing:
        missing_df = df[df["composite_rds_cluster_id"].isna()].copy()
        report_cols = [c for c in ("region_id", "Region", "area_id", "Area", "distributor_id", "House",
                                   "territory_id", "Territory", "point_id", "Point", "rds_point_id",
                                   "Cluster_Id", "Cluster_Name", "rds_cluster_id", "route_section_id",
                                   "rds_route_id", "point_cl_rt_iden", "composite_rds_cluster_id",
                                   "composite_rds_route_id") if c in missing_df.columns]
        if opts.write_csv:
            _report(missing_df[report_cols], os.path.join(opts.save_dir, "missing_composite_cluster_route_id.csv"))
        distinct = missing_df.drop_duplicates(subset=["Cluster_Id", "Cluster_Name", "rds_route_id", "point_cl_rt_iden"])
        per = distinct.groupby("Cluster_Id")["rds_route_id"].nunique()
        if (per > 1).any():
            raise RuntimeError("A Cluster_Id maps to multiple rds_route_id; cannot insert safely.")
        insert_and_backfill(df, missing_df, cfg, opts, source_id_col="Cluster_Id", name_col="Cluster_Name",
                            parent_col="rds_route_id", id_col="composite_rds_cluster_id", level="cluster",
                            dedup_cols=["Cluster_Id", "Cluster_Name", "rds_route_id", "point_cl_rt_iden"],
                            upload_fname="new_cluster_upload.csv")
    df["rds_cluster_id"] = df["composite_rds_cluster_id"]
    return df


def stage_outlet(df, cfg, opts: Opts):
    import pandas as pd
    old = pd.read_csv(cfg.old_path, usecols=["Outlet_Code", "rds_outlet_id"], dtype=str)
    lookup = build_lookup(old, "Outlet_Code", "rds_outlet_id", normalize_fn=normalize)
    filled, missing = apply_lookup(df, "Outlet_Code", "rds_outlet_id", lookup, normalize_fn=normalize)
    opts.record("outlet", filled, missing)
    if missing:
        missing_df = df[df["rds_outlet_id"].isna()].copy()
        if opts.write_csv:
            _report(missing_df, os.path.join(opts.save_dir, "missing_outlet_id.csv"))
        insert_and_backfill(df, missing_df, cfg, opts, source_id_col="Outlet_Code", name_col="Outlet_Name",
                            parent_col="rds_cluster_id", id_col="rds_outlet_id", level="outlet",
                            dedup_cols=["Outlet_Code", "rds_cluster_id"],
                            upload_fname="new_outlet_upload.csv")
    return df


# ---- Reconciliation & verification ----
def reconcile(df, opts: Opts) -> int:
    have = df["rds_route_id"].notna() & df["composite_rds_route_id"].notna()
    mismatch = have & (df["rds_route_id"] != df["composite_rds_route_id"])
    n = int(mismatch.sum())
    if n:
        log.warning("Reconcile: %d row(s) where rds_route_id != composite_rds_route_id", n)
        if opts.write_csv:
            _report(df[mismatch], os.path.join(opts.save_dir, "reconcile_warnings.csv"))
    else:
        log.info("Reconcile: rds_route_id matches composite_rds_route_id everywhere.")
    return n


def verify(df, opts: Opts) -> dict:
    cols = ["rds_point_id", "rds_outlet_id", "rds_route_id", "rds_cluster_id"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    counts = df[cols].isna().sum()
    log.info("VERIFY isna().sum(): %s", counts.to_dict())
    n = int(counts.sum())
    if n:
        raise RuntimeError(f"Verification FAILED: {n} row(s) still missing an id ({counts.to_dict()}).")
    return counts.to_dict()


def print_summary(opts: Opts) -> None:
    if not opts.stats:
        return
    print("\nStage summary")
    print(f"{'stage':<10}{'filled':>12}{'missing':>12}")
    for stage, s in opts.stats.items():
        print(f"{stage:<10}{s['filled']:>12}{s['missing']:>12}")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)


# --------------------------------------------------------------------------- #
# Dependency gate
# --------------------------------------------------------------------------- #
def _import_name(pip: str) -> str:
    if pip == "psycopg2-binary":
        return "psycopg2"
    return pip.replace("-", "_")


def _missing() -> list[str]:
    out = []
    for mod, pip in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            out.append(pip)
    return out


def _pip(package: str) -> int:
    return subprocess.run([sys.executable, "-m", "pip", "install", "--progress-bar", "off", package]).returncode


def ensure_dependencies() -> bool:
    missing = _missing()
    if not missing:
        return True
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception:
        print("Missing libraries: " + ", ".join(missing))
        return all(_pip(p) == 0 for p in missing)
    root = tk.Tk(); root.withdraw()
    if not messagebox.askyesno(
        "Missing libraries",
        "This app requires:\n\n- " + "\n- ".join(missing) + "\n\nInstall them now?\n(No = exit.)",
    ):
        root.destroy(); return False
    root.deiconify(); root.title("Installing libraries")
    root.geometry("540x220")
    tk.Label(root, text="Installing required libraries...", font=("Segoe UI", 11)).pack(pady=18)
    bar = ttk.Progressbar(root, length=480, maximum=len(missing)); bar.pack(pady=8)
    lab = tk.Label(root, text=""); lab.pack(); root.update()
    failed = False
    for i, pkg in enumerate(missing):
        lab["text"] = f"[{i+1}/{len(missing)}] Installing {pkg} ..."
        root.update()
        failed |= _pip(pkg) != 0
        bar["value"] = i + 1; root.update()
    root.destroy()
    return not failed


# --------------------------------------------------------------------------- #
# Glass GUI (PySide6 — imported after the gate)
# --------------------------------------------------------------------------- #
def run_app() -> int:
    from PySide6.QtCore import Qt, QThread, QObject, Signal
    from PySide6.QtGui import QPainter, QColor, QLinearGradient, QPen, QPainterPath, QFont
    from PySide6.QtWidgets import (
        QApplication, QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout,
        QPushButton, QCheckBox, QLineEdit, QTextEdit, QStackedWidget, QProgressBar,
        QGraphicsDropShadowEffect, QFileDialog, QMessageBox, QSizeGrip, QScrollArea,
    )

    class Bridge(QObject):
        sig = Signal(str)

    bridge = Bridge()

    class EmitterHandler(logging.Handler):
        def emit(self, record):
            try:
                bridge.sig.emit(self.format(record))
            except Exception:
                pass

    handler = EmitterHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    class GlassPanel(QFrame):
        def __init__(self, radius=18, parent=None):
            super().__init__(parent)
            self.radius = radius
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(26); eff.setOffset(0, 6); eff.setColor(QColor(0, 0, 0, 90))
            self.setGraphicsEffect(eff)

        def paintEvent(self, ev):
            p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), self.radius, self.radius)
            p.fillPath(path, QColor(255, 255, 255, 22))
            p.setPen(QPen(QColor(255, 255, 255, 70), 1)); p.drawPath(path); p.end()

    class GradWindow(QWidget):
        def paintEvent(self, ev):
            p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
            g = QLinearGradient(0, 0, self.width(), self.height())
            g.setColorAt(0.0, QColor(18, 22, 38)); g.setColorAt(0.55, QColor(30, 34, 56))
            g.setColorAt(1.0, QColor(12, 14, 26)); p.fillRect(self.rect(), g)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(120, 160, 255, 26)); p.drawEllipse(self.width() * 0.62, -60, 340, 340)
            p.setBrush(QColor(90, 220, 200, 18)); p.drawEllipse(-90, self.height() * 0.55, 320, 320)
            p.end()

    class BarChart(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.stats = {}; self.setMinimumHeight(300)

        def set_stats(self, stats):
            self.stats = stats or {}; self.update()

        def paintEvent(self, ev):
            p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            pl, pr, pt, pb = 90, 24, 54, 62
            pw, ph = max(w - pl - pr, 40), max(h - pt - pb, 40); base = pt + ph
            p.setFont(QFont("Segoe UI", 12, QFont.Bold)); p.setPen(QColor(255, 255, 255, 235))
            p.drawText(0, 16, w, 30, Qt.AlignHCenter, "Matched vs Missing per Stage")
            p.setFont(QFont("Segoe UI", 10))
            if not self.stats:
                p.setPen(QColor(200, 200, 200, 180))
                inner = QPainterPath(); inner.addRoundedRect(pl, pt, pw, ph, 12, 12)
                p.setPen(QPen(QColor(255, 255, 255, 40), 1)); p.drawPath(inner)
                p.drawText(pl, pt, pw, ph, Qt.AlignCenter, "Run the pipeline to draw the chart.")
                return
            stages = [s for s in STAGE_ORDER if s in self.stats]
            maxv = max([int(self.stats[s][k]) for s in stages for k in ("filled", "missing")] or [1])
            maxv = max(maxv, 1)
            gw, bw, gap = pw / len(stages), pw / len(stages) * 0.28, pw / len(stages) * 0.10
            for i, s in enumerate(stages):
                cx = pl + gw * i + gw / 2
                filled = int(self.stats[s].get("filled", 0)); missing = int(self.stats[s].get("missing", 0))
                fh, mh = ph * filled / maxv, ph * missing / maxv
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(88, 210, 150, 225)); p.drawRoundedRect(int(cx - bw - gap / 2), int(base - fh), int(bw), int(fh), 4, 4)
                p.setBrush(QColor(230, 90, 120, 225)); p.drawRoundedRect(int(cx + gap / 2), int(base - mh), int(bw), int(mh), 4, 4)
                p.setFont(QFont("Segoe UI", 8, QFont.Bold)); p.setPen(QColor(255, 255, 255, 235))
                if filled: p.drawText(int(cx - bw - gap / 2), max(base - fh - 16, 4), int(bw), 16, Qt.AlignHCenter, str(filled))
                if missing: p.drawText(int(cx + gap / 2), max(base - mh - 16, 4), int(bw), 16, Qt.AlignHCenter, str(missing))
                p.setFont(QFont("Segoe UI", 10)); p.setPen(QColor(255, 255, 255, 235))
                p.drawText(int(cx - gw / 2), base + 10, int(gw), 22, Qt.AlignHCenter, STAGE_SHORT[s])
            p.setPen(QColor(200, 200, 200, 170))
            p.drawLine(int(pl), int(pt), int(pl), int(base)); p.drawLine(int(pl), int(base), int(pl + pw), int(base))
            for t in range(6):
                val = maxv * t / 5; y = int(base - ph * t / 5)
                p.drawText(0, y - 7, int(pl - 8), 14, Qt.AlignRight, f"{val:,.0f}")
            p.end()

    class Worker(QObject):
        log_line = Signal(str)
        done = Signal(dict, dict)
        okay = Signal(dict)
        failed = Signal(str)

        def __init__(self, mk):
            super().__init__(); self.mk = mk

        def run(self):
            import pandas as pd
            cfg, opts, stages = self.mk["cfg"], self.mk["opts"], self.mk["stages"]
            try:
                setup_logging()
                if handler not in logging.getLogger("migration").handlers:
                    logging.getLogger("migration").addHandler(handler)
                if opts.write_db:
                    opts.conn = connect(cfg)
                    self.log_line.emit(f"Connected to {cfg.table}")
                df = pd.read_csv(cfg.new_path, dtype=str)
                self.log_line.emit(f"Loaded {len(df)} rows from {cfg.new_path}")
                validate_inputs(df, cfg, opts)
                self.log_line.emit("Inputs validated (columns + outlet-code uniqueness).")
                for stage in stages:
                    self.log_line.emit(f"===== Stage: {stage} =====")
                    fn = {"point": stage_points, "key": stage_keys, "route": stage_route,
                          "cluster": stage_cluster, "outlet": stage_outlet}[stage]
                    df = fn(df, cfg, opts)
                    s = opts.stats.get(stage, {})
                    self.log_line.emit(f"  {stage}: filled={s.get('filled','?')} missing={s.get('missing','?')}")
                if not opts.dry_run:
                    rec = reconcile(df, opts)
                    if rec:
                        self.log_line.emit(f"Reconcile warnings: {rec} row(s) (review).")
                if opts.write_csv and not opts.dry_run:
                    df.to_csv(cfg.output_path, index=False)
                    self.log_line.emit(f"Saved output -> {cfg.output_path}")
                if not opts.dry_run:
                    verify(df, opts)
                    self.log_line.emit("Verification PASSED: all four id columns populated.")
                if opts.conn is not None:
                    opts.conn.commit(); opts.conn.close()
                    self.log_line.emit("DB transaction committed and connection closed.")
                self.done.emit(opts.stats, self.mk["context"])
            except Exception as exc:
                try:
                    if opts.conn is not None and not opts.dry_run:
                        opts.conn.rollback(); opts.conn.close()
                except Exception:
                    pass
                self.failed.emit(f"{exc}")

    class MainWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.resize(1120, 760)
            self.stats = {}
            self.cfg = load_config()
            self._drag = None
            self._build()
            bridge.sig.connect(self.append_log)

        def _btn(self, text, primary=False):
            b = QPushButton(text); b.setCursor(Qt.PointingHandCursor); b.setMinimumHeight(40)
            bg = "rgba(120,160,255,120)" if primary else "rgba(255,255,255,26)"
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:#e8ecff;border:1px solid rgba(255,255,255,70);"
                f"border-radius:12px;font:14px 'Segoe UI';padding:8px 16px;}}"
                f"QPushButton:hover{{background:rgba(160,190,255,90);}}"
                f"QPushButton:disabled{{color:#889;}}")
            return b

        def _lbl(self, text, size=12, color="#c8d3ff", bold=False):
            w = QLabel(text)
            w.setStyleSheet(f"color:{color};font:{size}px 'Segoe UI';" + ("font-weight:600;" if bold else ""))
            return w

        def _build(self):
            root = GradWindow(self)
            outer = QVBoxLayout(root); outer.setContentsMargins(24, 16, 24, 18)
            tr = QHBoxLayout()
            ti = QLabel("◆  Location Automation — RDS Migration")
            ti.setStyleSheet("color:#eef2ff;font:15px 'Segoe UI';font-weight:600;")
            tr.addWidget(ti); tr.addStretch(1)
            for txt, fn in (("—", self.showMinimized), ("✕", self.close)):
                b = QPushButton(txt); b.setCursor(Qt.PointingHandCursor); b.setFixedSize(32, 30)
                b.setStyleSheet("QPushButton{background:rgba(255,255,255,30);border:none;color:#e8ecff;"
                                "border-radius:8px;}QPushButton:hover{background:rgba(230,90,120,180);}")
                b.clicked.connect(fn); tr.addWidget(b)
            outer.addLayout(tr)
            body = QHBoxLayout(); body.setSpacing(16)
            side = GlassPanel(); side.setFixedWidth(214)
            sv = QVBoxLayout(side); sv.setContentsMargins(14, 18, 14, 18)
            self.nav = []
            for key, lbl in (("setup", "Setup"), ("run", "Run"), ("visual", "Visuals"),
                             ("log", "Log")):
                b = self._btn(lbl); b.clicked.connect(lambda c=False, k=key: self.switch(k))
                sv.addWidget(b); self.nav.append((key, b))
            sv.addStretch(1); body.addWidget(side)
            self.stack = QStackedWidget()
            self._page_setup(); self._page_run(); self._page_visual()
            self._page_log()
            body.addWidget(self.stack, 1); outer.addLayout(body, 1)
            outer.addWidget(QSizeGrip(self))
            lo = QVBoxLayout(self); lo.setContentsMargins(0, 0, 0, 0); lo.addWidget(root)
            self.switch("run")

        def _small(self, text="⋯"):
            b = QPushButton(text)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(42, 34)
            b.setStyleSheet("QPushButton{background:rgba(120,160,255,120);color:#e8ecff;border:none;"
                            "border-radius:8px;font:15px 'Segoe UI';}"
                            "QPushButton:hover{background:rgba(160,190,255,170);}")
            return b

        def _page_setup(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.setSpacing(10)
            lay.addWidget(self._lbl("Setup — files & database", 16, "#eef2ff", True))
            self.i_old = QLineEdit(self.cfg.old_file)
            self.i_new = QLineEdit(self.cfg.new_file)
            self.i_out = QLineEdit(self.cfg.output_file)
            self.i_save = QLineEdit(self.cfg.save_dir)
            self.i_table = QLineEdit(self.cfg.table)
            grid = QGridLayout()
            rows = (("Old mapped file", self.i_old, self._browse_old),
                    ("New fresh extract", self.i_new, self._browse_new),
                    ("Output file", self.i_out, self._browse_out),
                    ("Reports dir", self.i_save, self._browse_save),
                    ("Table", self.i_table, None))
            for i, (lab, w, fn) in enumerate(rows):
                grid.addWidget(self._lbl(lab), i, 0)
                grid.addWidget(w, i, 1)
                if fn:
                    bb = self._small(); bb.clicked.connect(fn); grid.addWidget(bb, i, 2)
            grid.setColumnStretch(1, 1); lay.addLayout(grid)
            lay.addWidget(self._lbl("PostgreSQL (used only when 'Write to DB' is on)", 12, "#eef2ff", True))
            self.i_host, self.i_port, self.i_name, self.i_user, self.i_pwd = (
                QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit())
            self.i_host.setText(self.cfg.db_host or "localhost")
            self.i_port.setText(str(self.cfg.db_port or 5432))
            self.i_name.setText(self.cfg.db_name); self.i_user.setText(self.cfg.db_user)
            self.i_pwd.setText(self.cfg.db_password); self.i_pwd.setEchoMode(QLineEdit.Password)
            gd = QGridLayout()
            for i, (lab, w) in enumerate((("Host", self.i_host), ("Port", self.i_port),
                                          ("Database", self.i_name), ("User", self.i_user),
                                          ("Password", self.i_pwd))):
                gd.addWidget(self._lbl(lab), i, 0); gd.addWidget(w, i, 1)
            lay.addLayout(gd)
            hb = QHBoxLayout()
            save_b = self._btn("Save config.json", True); save_b.clicked.connect(self._save_cfg)
            load_b = self._btn("Load config.json"); load_b.clicked.connect(self._load_cfg)
            hb.addWidget(save_b); hb.addWidget(load_b)
            lay.addLayout(hb); lay.addStretch(1)
            self.stack.addWidget(panel)

        def _page_run(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.addWidget(self._lbl("Run — pipeline stages", 16, "#eef2ff", True))
            row = QHBoxLayout(); self.stage_vars = {}
            for s in STAGE_ORDER:
                c = QCheckBox(STAGE_SHORT[s]); c.setChecked(True)
                c.setStyleSheet("QCheckBox{color:#e8ecff;font:13px 'Segoe UI';spacing:6px;}")
                self.stage_vars[s] = c; row.addWidget(c)
            lay.addLayout(row)
            opts = QHBoxLayout()
            self.c_dry = QCheckBox("Dry run (no writes)"); self.c_write = QCheckBox("Write to DB")
            for c in (self.c_dry, self.c_write):
                c.setChecked(c is self.c_dry); c.setStyleSheet("QCheckBox{color:#e8ecff;font:13px 'Segoe UI';}")
            opts.addWidget(self.c_dry); opts.addWidget(self.c_write); opts.addStretch(1)
            lay.addLayout(opts)
            self.btn_run = self._btn("Run pipeline", True); self.btn_stop = self._btn("Stop")
            self.btn_stop.setEnabled(False)
            self.btn_run.clicked.connect(self.start_run); self.btn_stop.clicked.connect(self.stop_run)
            hb = QHBoxLayout(); hb.addWidget(self.btn_run); hb.addWidget(self.btn_stop); hb.addStretch(1)
            lay.addLayout(hb)
            self.run_progress = QProgressBar(); self.run_progress.setRange(0, 100)
            self.run_progress.setFixedHeight(10); self.run_progress.setTextVisible(False)
            self.run_progress.setStyleSheet("QProgressBar{background:rgba(255,255,255,30);border:none;"
                                            "border-radius:5px;}QProgressBar::chunk{background:#5bd1a0;border-radius:5px;}")
            lay.addWidget(self.run_progress)
            self.run_status_label = self._lbl("Status: ready", 12, "#a9b2ff")
            lay.addWidget(self.run_status_label); lay.addStretch(1)
            self.stack.addWidget(panel)

        def _page_visual(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.addWidget(self._lbl("Visuals", 16, "#eef2ff", True))
            self.chart = BarChart(); lay.addWidget(self.chart, 1)
            self.stack.addWidget(panel)

        def _page_log(self):
            panel = GlassPanel(); lay = QVBoxLayout(panel); lay.setContentsMargins(22, 22, 22, 22)
            lay.addWidget(self._lbl("Log", 16, "#eef2ff", True))
            self.log_text = QTextEdit(); self.log_text.setReadOnly(True)
            self.log_text.setStyleSheet("background:rgba(0,0,0,70);color:#e4ecff;border:none;"
                                        "border-radius:12px;font:11px 'Consolas';padding:10px;")
            lay.addWidget(self.log_text, 1)
            h = QHBoxLayout(); h.addStretch(1)
            clr = self._btn("Clear log"); clr.clicked.connect(lambda: self.log_text.clear()); h.addWidget(clr)
            lay.addLayout(h); self.stack.addWidget(panel)

        def _browse_old(self):
            p, _ = QFileDialog.getOpenFileName(self, "Select old mapped file",
                                               os.path.dirname(self.i_old.text()) or str(BASE_DIR))
            if p:
                self.i_old.setText(p)

        def _browse_new(self):
            p, _ = QFileDialog.getOpenFileName(self, "Select new fresh extract",
                                               os.path.dirname(self.i_new.text()) or str(BASE_DIR))
            if p:
                self.i_new.setText(p)
                default_out = "data/retailers_05_07_2026_update.csv"
                if self.i_out.text().strip() in ("", default_out):
                    base, ext = os.path.splitext(p)
                    self.i_out.setText(base + "_update" + ext)

        def _browse_out(self):
            p, _ = QFileDialog.getSaveFileName(self, "Select output file",
                                               self.i_out.text() or str(BASE_DIR))
            if p:
                self.i_out.setText(p)

        def _browse_save(self):
            d = QFileDialog.getExistingDirectory(self, "Select reports directory",
                                                 self.i_save.text() or str(BASE_DIR))
            if d:
                self.i_save.setText(d)

        def _save_cfg(self):
            try:
                cfg = self._collect_cfg()
                CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2, default=str), encoding="utf-8")
                self._set_status(f"Config saved -> {CONFIG_FILE}")
            except Exception as exc:
                self._set_status(f"Config save failed: {exc}")

        def _load_cfg(self):
            try:
                self.cfg = load_config()
                self.i_old.setText(self.cfg.old_file)
                self.i_new.setText(self.cfg.new_file)
                self.i_out.setText(self.cfg.output_file)
                self.i_save.setText(self.cfg.save_dir)
                self.i_table.setText(self.cfg.table)
                self.i_host.setText(self.cfg.db_host or "localhost")
                self.i_port.setText(str(self.cfg.db_port or 5432))
                self.i_name.setText(self.cfg.db_name)
                self.i_user.setText(self.cfg.db_user)
                self.i_pwd.setText(self.cfg.db_password)
                self._set_status(f"Config loaded from {CONFIG_FILE}")
            except Exception as exc:
                self._set_status(f"Config load failed: {exc}")

        def _collect_cfg(self):
            c = Config()
            c.old_file, c.new_file = self.i_old.text(), self.i_new.text()
            c.output_file, c.save_dir = self.i_out.text(), self.i_save.text()
            c.table = self.i_table.text()
            c.db_host = self.i_host.text(); c.db_port = int(self.i_port.text() or 5432)
            c.db_name = self.i_name.text(); c.db_user = self.i_user.text(); c.db_password = self.i_pwd.text()
            return c

        def _set_status(self, text):
            if getattr(self, "run_status_label", None) and self.run_status_label is not None:
                self.run_status_label.setText("Status: " + text)

        def start_run(self):
            stages = [s for s in STAGE_ORDER if self.stage_vars[s].isChecked()]
            if not stages:
                QMessageBox.warning(self, "Run", "Select at least one stage.")
                return
            dry = self.c_dry.isChecked(); write = self.c_write.isChecked() and not dry
            cfg = self._collect_cfg()
            opts = Opts(dry_run=dry, write_db=write, write_csv=not dry, conn=None, save_dir=cfg.save_path)
            context = {k: getattr(cfg, k) for k in ("old_file", "new_file", "output_file", "table")}
            mk = {"cfg": cfg, "opts": opts, "stages": stages, "context": context}
            self.thread = QThread(); self.worker = Worker(mk)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.done.connect(self._on_done)
            self.worker.failed.connect(self._on_fail)
            self.worker.log_line.connect(self.append_log)
            self.worker.done.connect(self.thread.quit)
            self.worker.failed.connect(self.thread.quit)
            self.btn_run.setEnabled(False); self.btn_stop.setEnabled(True)
            self.run_progress.setRange(0, 0); self._set_status("Running...")
            self.thread.start()

        def _on_done(self, stats, context):
            self.stats = stats; self.chart.set_stats(stats)
            self.run_progress.setRange(0, 100); self.run_progress.setValue(100)
            self._set_status("Done."); self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)

        def _on_fail(self, msg):
            self.append_log("ERROR: " + msg)
            QMessageBox.critical(self, "Error", msg)
            self.run_progress.setRange(0, 100); self.run_progress.setValue(0)
            self._set_status(f"Error: {msg}"); self.btn_run.setEnabled(True); self.btn_stop.setEnabled(False)

        def stop_run(self):
            if self.thread is not None and self.thread.isRunning():
                self.thread.requestInterruption(); self._set_status("Stopped.")

        def switch(self, key):
            idx = ["setup", "run", "visual", "log"].index(key)
            self.stack.setCurrentIndex(idx)
            for k, b in self.nav:
                b.setStyleSheet(
                    "QPushButton{background:rgba(255,255,255,26);color:#e8ecff;"
                    "border:1px solid rgba(255,255,255,70);border-radius:12px;"
                    "font:14px 'Segoe UI';padding:8px 16px;}"
                    "QPushButton:hover{background:rgba(160,190,255,90);}"
                    "QPushButton:disabled{color:#889;}")

        def append_log(self, text):
            self.log_text.append(text)

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._drag = e.globalPosition().toPoint()
            super().mousePressEvent(e)

        def mouseMoveEvent(self, e):
            if self._drag is not None and e.buttons() & Qt.LeftButton:
                self.move(self.pos() + e.globalPosition().toPoint() - self._drag)
                self._drag = e.globalPosition().toPoint()
            super().mouseMoveEvent(e)

        def mouseReleaseEvent(self, e):
            self._drag = None; super().mouseReleaseEvent(e)

    app = QApplication(sys.argv)
    app.setApplicationName("Location_Automation_full")
    win = MainWindow()
    win.show()
    return app.exec()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    if not ensure_dependencies():
        print("Required libraries are missing and were not installed. Exit.")
        return 1
    print("Dependencies OK. Launching glass UI ...")
    return run_app()


if __name__ == "__main__":
    sys.exit(main())