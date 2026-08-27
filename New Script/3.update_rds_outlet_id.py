"""Stage OUTLET — Resolve rds_outlet_id by Outlet_Code; insert new outlets.

Standalone usage:
    python "3.update_rds_outlet_id.py" [--dry-run] [--write-db]

Requires rds_cluster_id to be resolved first (cluster stage). Normalled the
pipeline runs route -> cluster -> outlet, so run this AFTER 4 and 5.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import migration_common as mc
from config import load_config


def run(args) -> None:
    mc.setup_logging()
    cfg = load_config(args.config)
    opts = mc.build_opts(cfg, args)
    df = pd.read_csv(cfg.output_path, dtype=str)
    df = mc.stage_outlet(df, cfg, opts)
    if opts.write_csv and not opts.dry_run:
        df.to_csv(cfg.output_path, index=False)
        print(f"Saved output -> {cfg.output_path}")
    else:
        print("Dry run / write disabled: output CSV not written.")
    mc.print_summary(opts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage outlet: resolve rds_outlet_id.")
    mc.add_common_args(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
