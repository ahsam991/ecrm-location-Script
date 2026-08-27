"""Stage 2 — Build the three identifier columns (pure construction).

Standalone usage:
    python "2.update_3_identifier_column.py" [--dry-run]

Normally run through run_pipeline.py. No DB access.
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
    df = mc.stage_keys(df, cfg, opts)
    if opts.write_csv and not opts.dry_run:
        df.to_csv(cfg.output_path, index=False)
        print(f"Saved output -> {cfg.output_path}")
    else:
        print("Dry run / write disabled: output CSV not written.")
    mc.print_summary(opts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 2: build identifier columns.")
    mc.add_common_args(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
