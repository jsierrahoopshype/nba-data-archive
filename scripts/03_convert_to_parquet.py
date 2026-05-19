"""
03_convert_to_parquet.py

Converts the .tar.xz archives in data/ into Parquet files for downstream
tools. Produces two layouts:

  parquet/per_season/<datatype>/<season>.parquet
  parquet/per_season/<datatype>/po_<season>.parquet
  parquet/merged/<datatype>.parquet   (all seasons + season types in one)

Every row in the per_season files gets two provenance columns added:
  _season       int   (e.g. 2024 for the 2024/25 season)
  _season_type  str   ("rg" or "po")

The merged files keep those columns so downstream tools can filter by season
without parsing filenames.

Resumable: per-season Parquet files that already exist are skipped.
Merged Parquet files are always rewritten (cheap, ensures consistency with
the per-season set).

Memory-safe: the merged writes stream chunk by chunk using ParquetWriter
instead of loading the whole concatenation into RAM. That matters for
nbastats (~18M rows across 30 seasons).

Usage:
  python scripts/03_convert_to_parquet.py
"""

from __future__ import annotations

import io
import re
import sys
import tarfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
PARQUET_DIR = REPO_ROOT / "parquet"
PER_SEASON_DIR = PARQUET_DIR / "per_season"
MERGED_DIR = PARQUET_DIR / "merged"

DATA_TYPES = (
    "shotdetail",
    "matchups",
    "nbastats",
    "nbastatsv3",
    "pbpstats",
    "datanba",
    "cdnnba",
)

# Filename parser:
#   shotdetail_1996.tar.xz       -> datatype=shotdetail season=1996 type=rg
#   pbpstats_po_2023.tar.xz      -> datatype=pbpstats   season=2023 type=po
NAME_RE = re.compile(
    r"^(?P<datatype>[a-z0-9]+?)(?:_(?P<seasontype>po))?_(?P<season>\d{4})$"
)


def parse_name(stem: str) -> tuple[str, int, str] | None:
    """Returns (datatype, season, seasontype) or None if it doesn't parse."""
    m = NAME_RE.match(stem)
    if not m:
        return None
    return (
        m.group("datatype"),
        int(m.group("season")),
        m.group("seasontype") or "rg",
    )


def per_season_path(datatype: str, season: int, seasontype: str) -> Path:
    folder = PER_SEASON_DIR / datatype
    folder.mkdir(parents=True, exist_ok=True)
    if seasontype == "po":
        return folder / f"po_{season}.parquet"
    return folder / f"{season}.parquet"


def merged_path(datatype: str) -> Path:
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    return MERGED_DIR / f"{datatype}.parquet"


def extract_csv_to_dataframe(archive: Path, expected_csv_name: str) -> pd.DataFrame:
    """Open a tar.xz, find its CSV member, return as DataFrame."""
    with tarfile.open(archive, mode="r:xz") as tar:
        # The archive should contain a CSV named after the archive (without .tar.xz)
        member = None
        for m in tar.getmembers():
            if m.name == expected_csv_name or m.name.endswith("/" + expected_csv_name):
                member = m
                break
        if member is None:
            # Fall back to any .csv inside the archive.
            for m in tar.getmembers():
                if m.isfile() and m.name.lower().endswith(".csv"):
                    member = m
                    break
        if member is None:
            raise RuntimeError(f"No CSV found inside {archive.name}")

        fh = tar.extractfile(member)
        if fh is None:
            raise RuntimeError(f"Could not open {member.name} inside {archive.name}")
        data = fh.read()

    # Some upstream files use encoding quirks; latin-1 is the safe parse.
    return pd.read_csv(
        io.BytesIO(data),
        low_memory=False,
        encoding="latin-1",
    )


def convert_one(archive: Path) -> tuple[str, str]:
    """Convert one .tar.xz to its per-season Parquet. Returns (status, message)."""
    stem = archive.name.removesuffix(".tar.xz")
    parsed = parse_name(stem)
    if not parsed:
        return ("skipped", f"unparseable name: {archive.name}")

    datatype, season, seasontype = parsed
    if datatype not in DATA_TYPES:
        return ("skipped", f"unknown datatype: {datatype}")

    out_path = per_season_path(datatype, season, seasontype)
    if out_path.exists() and out_path.stat().st_size > 0:
        return ("skipped_existing", str(out_path))

    expected_csv = f"{stem}.csv"
    try:
        df = extract_csv_to_dataframe(archive, expected_csv)
    except Exception as e:
        return ("failed", f"{archive.name}: {e}")

    df["_season"] = season
    df["_season_type"] = seasontype

    try:
        # Snappy compression, ZSTD would shrink a bit more but snappy is the
        # standard and reads fast in every Parquet client.
        df.to_parquet(out_path, engine="pyarrow", compression="snappy", index=False)
    except Exception as e:
        if out_path.exists():
            out_path.unlink()
        return ("failed", f"{archive.name} write: {e}")

    return ("converted", str(out_path))


def build_merged(datatype: str) -> tuple[str, str]:
    """Stream-concatenate all per-season files of a datatype into one Parquet."""
    folder = PER_SEASON_DIR / datatype
    if not folder.exists():
        return ("skipped", f"no per-season folder for {datatype}")

    season_files = sorted(folder.glob("*.parquet"))
    if not season_files:
        return ("skipped", f"no per-season files for {datatype}")

    # Pass 1: build unified schema across all seasons (columns evolve over time).
    schemas = [pq.read_schema(f) for f in season_files]
    unified = pa.unify_schemas(schemas, promote_options="default")

    out_path = merged_path(datatype)

    # Pass 2: stream-write each season into the merged file, casting to unified.
    with pq.ParquetWriter(out_path, unified, compression="snappy") as writer:
        for f in season_files:
            table = pq.read_table(f)
            # Add missing columns as null arrays.
            for field in unified:
                if field.name not in table.column_names:
                    table = table.append_column(
                        field.name,
                        pa.nulls(table.num_rows, type=field.type),
                    )
            # Reorder columns to match the unified schema.
            table = table.select(unified.names)
            writer.write_table(table)

    return ("merged", str(out_path))


def main() -> int:
    if not DATA_DIR.exists():
        print(f"ERROR: data folder not found: {DATA_DIR}", file=sys.stderr)
        print("Run 01_download_archives.py first.", file=sys.stderr)
        return 1

    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    PER_SEASON_DIR.mkdir(parents=True, exist_ok=True)
    MERGED_DIR.mkdir(parents=True, exist_ok=True)

    archives = sorted(DATA_DIR.glob("*.tar.xz"))
    print(f"Source archives:  {len(archives)} files in {DATA_DIR}")
    print(f"Per-season out:   {PER_SEASON_DIR}")
    print(f"Merged out:       {MERGED_DIR}")
    print()

    # ---- Pass A: per-season Parquet ----
    counts = {"converted": 0, "skipped_existing": 0, "skipped": 0, "failed": 0}
    for arc in tqdm(archives, desc="per-season", unit="file"):
        status, msg = convert_one(arc)
        counts[status] = counts.get(status, 0) + 1
        if status == "failed":
            tqdm.write(f"  FAILED: {arc.name} — {msg}")

    print()
    print("Per-season summary:")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")

    if counts.get("failed", 0):
        print()
        print(
            "Some per-season conversions failed. "
            "Re-run to retry, or inspect the listed archives.",
            file=sys.stderr,
        )

    # ---- Pass B: merged Parquet per data type ----
    print()
    print("Building merged Parquet per data type...")
    merged_results: dict[str, tuple[str, str]] = {}
    for dtype in tqdm(DATA_TYPES, desc="merged", unit="type"):
        try:
            merged_results[dtype] = build_merged(dtype)
        except Exception as e:
            merged_results[dtype] = ("failed", f"{dtype}: {e}")

    print()
    print("Merged summary:")
    for dtype, (status, msg) in merged_results.items():
        print(f"  {dtype:15s} {status:10s} {msg}")

    failed_total = counts.get("failed", 0) + sum(
        1 for s, _ in merged_results.values() if s == "failed"
    )
    return 0 if failed_total == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
