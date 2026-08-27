"""Run the whole Retailer Location Migration pipeline in the correct order.

Stages run top-down so each new row's parent is already resolved:
    point -> keys -> route -> cluster -> outlet -> verify

Usage examples:
    python run_pipeline.py --dry-run
    python run_pipeline.py --write-db
    python run_pipeline.py --stage route --write-db
    python run_pipeline.py --stage outlet --skip-stage route --dry-run
"""
import argparse
import sys

import pandas as pd

import migration_common as mc
from config import load_config

STAGE_ORDER = ["point", "key", "route", "cluster", "outlet"]


def build_stage_runner(stage: str):
    return {
        "point": mc.stage_points,
        "key": mc.stage_keys,
        "route": mc.stage_route,
        "cluster": mc.stage_cluster,
        "outlet": mc.stage_outlet,
    }[stage]


def run(args) -> None:
    mc.setup_logging()
    cfg = load_config(args.config)
    opts = mc.build_opts(cfg, args)

    if args.write_db and not args.dry_run:
        opts.conn = mc.connect(cfg, dry_run=False)
        log = mc.log
        log.info("Connected to %s", cfg.table)

    skip = set(args.skip_stage or [])
    only = set([args.stage]) if args.stage else set(STAGE_ORDER)

    # The fresh extract feeds the first stage; later stages consume the running
    # output. To keep one in-memory pass we start from the fresh extract.
    df = pd.read_csv(cfg.new_path, dtype=str)
    if opts.limit > 0:
        df = df.head(opts.limit).copy()

    try:
        for stage in STAGE_ORDER:
            if stage in skip or stage not in only:
                continue
            print(f"\n===== Stage: {stage} =====")
            df = build_stage_runner(stage)(df, cfg, opts)

        mc.print_summary(opts)

        if not opts.dry_run:
            n = mc.reconcile(df, opts)
            if n > 0:
                print(f"\nReconciliation warnings written ({n} row(s)); review before signing off.")

        if opts.write_csv and not opts.dry_run:
            df.to_csv(cfg.output_path, index=False)
            print(f"\nSaved output -> {cfg.output_path}")
        else:
            print("\nDry run / write disabled: output CSV not written.")

        if not opts.dry_run:
            mc.verify(df, opts)
            print("\nVerification passed: all four id columns are fully populated.")

        if opts.conn is not None:
            opts.conn.commit()
            opts.conn.close()
            print("\nDB transaction committed and connection closed.")
    except Exception:
        if opts.conn is not None:
            try:
                opts.conn.rollback()
            finally:
                opts.conn.close()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Retailer Location Migration pipeline.")
    mc.add_common_args(parser)
    parser.add_argument("--stage", choices=STAGE_ORDER, default=None,
                        help="Run only this stage.")
    parser.add_argument("--skip-stage", choices=STAGE_ORDER, action="append",
                        help="Skip a stage (repeatable).")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
