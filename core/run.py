"""CLI entry point for the Serif take-home rate matcher.

Usage:
    python -m core.run \
        --tic data/tic_extract_20250213.csv \
        --hpt data/hpt_extract_20250213.csv \
        --out out/unified_rates.csv

Reads the two input CSVs, runs the two-pass matcher, writes:
    - <out>                       unified per-rate rows (matched + unmatched)
    - <out>.stats.json            pipeline counters (filters, match levels)

Design notes:
    - dtype=str on read to preserve leading zeros in codes / EIN / license_number
    - Output is a single flat CSV so an analyst can pivot directly
      (no parquet dependency on a take-home reviewer's machine)
    - Stats are emitted separately so the README can quote real numbers
      and the reviewer can spot-check filter counts
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from core.match import run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match Transparency-in-Coverage rates to Hospital Price Transparency rates."
    )
    here = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--tic",
        type=Path,
        default=here / "data" / "tic_extract_20250213.csv",
        help="Path to TiC (payer) extract CSV.",
    )
    parser.add_argument(
        "--hpt",
        type=Path,
        default=here / "data" / "hpt_extract_20250213.csv",
        help="Path to HPT (hospital) extract CSV.",
    )
    parser.add_argument(
        "--aliases",
        type=Path,
        default=here / "core" / "payer_aliases.json",
        help="Path to payer alias JSON.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=here / "out" / "unified_rates.csv",
        help="Output CSV path. A sibling <out>.stats.json file is also written.",
    )
    return parser.parse_args(argv)


def _print_summary(stats_dict: dict) -> None:
    """Human-readable summary to stdout. PipelineStats is a flat dataclass."""
    print()
    print("=" * 60)
    print("Pipeline summary")
    print("=" * 60)
    width = 42
    for key, val in stats_dict.items():
        print(f"{key:<{width}} {val}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    for label, path in (("--tic", args.tic), ("--hpt", args.hpt), ("--aliases", args.aliases)):
        if not path.exists():
            print(f"ERROR: {label} not found: {path}", file=sys.stderr)
            return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)

    df, stats = run(args.tic, args.hpt, args.aliases)

    df.to_csv(args.out, index=False)

    stats_path = args.out.with_suffix(args.out.suffix + ".stats.json")
    stats_dict = asdict(stats)
    stats_path.write_text(json.dumps(stats_dict, indent=2, sort_keys=True))

    print(f"Wrote {len(df):,} rows -> {args.out}")
    print(f"Wrote stats         -> {stats_path}")
    _print_summary(stats_dict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
