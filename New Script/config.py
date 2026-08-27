"""Central configuration for the Retailer Location Migration pipeline.

Paths default to the repo layout. Override runtime paths or DB credentials via
the environment (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD or DB_DSN) or a
config.json place next to this file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Scripts live in the "New Script" subfolder; project root is one level up so
# that data/ and finded_data/ resolve relative to the project root.
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "config.json"


@dataclass
class Config:
    # Input / output extracts (all relative to BASE_DIR unless absolute).
    old_file: str = "data/retailers_02_05_2026_update.csv"
    new_file: str = "data/retailers_05_07_2026.csv"
    output_file: str = "data/retailers_05_07_2026_update.csv"
    save_dir: str = "finded_data"

    # Destination table for new locations.
    table: str = "ecrm.locations"

    # ecrm.locations `type` codes (from observed upload files).
    point_type: int = 5
    route_type: int = 6
    cluster_type: int = 7
    outlet_type: int = 8

    # PostgreSQL connection. Env vars override these.
    db_host: str = ""
    db_port: int = 5432
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""

    # Columns the pipeline adds/resolves (destination id columns), in order.
    target_columns: tuple = (
        "rds_outlet_id",
        "point_cl_rt_iden",
        "point_cl_iden",
        "point_rt_iden",
        "rds_cluster_id",
        "rds_route_id",
        "rds_point_id",
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
        return {
            "point": self.point_type,
            "route": self.route_type,
            "cluster": self.cluster_type,
            "outlet": self.outlet_type,
        }[level]


def _env_overrides(cfg: Config) -> Config:
    cfg.db_host = os.getenv("DB_HOST", cfg.db_host)
    cfg.db_port = int(os.getenv("DB_PORT", cfg.db_port))
    cfg.db_name = os.getenv("DB_NAME", cfg.db_name)
    cfg.db_user = os.getenv("DB_USER", cfg.db_user)
    cfg.db_password = os.getenv("DB_PASSWORD", cfg.db_password)
    return cfg


def load_config(path: str | None = None) -> Config:
    """Load config, applying defaults -> config.json -> env overrides."""
    cfg = Config()
    cfg_path = Path(path) if path else CONFIG_FILE
    if cfg_path.exists():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        known = set(asdict(Config()).keys())
        filtered = {k: v for k, v in data.items() if k in known}
        for key, value in filtered.items():
            if key == "target_columns":
                value = tuple(value)
            setattr(cfg, key, value)
    return _env_overrides(cfg)
