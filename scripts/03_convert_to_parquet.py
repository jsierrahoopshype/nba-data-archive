"""
03_convert_to_parquet.py

Converts the .tar.xz archives in data/ into Parquet files for downstream
tools. Produces two layouts:

  parquet/per_season/<datatype>/<season>.parquet
  parquet/per_season/<datatype>/po_<season>.parquet
  parquet/merged/<datatype>.parquet

Per-season files include _season (int) and _season_type ("rg"/"po") columns.

Resumable: per-season Parquet files that already exist are skipped.
Merged files are always rebuilt (cheap, keeps merged consistent with the
per-season set).

Schema reconciliation: across 30 seasons, columns drift. A column that's
int64 in one season may be float64 in another (because the season had
nulls and pandas inferred float). pyarrow.unify_schemas refuses these.
We use a custom reconciler that promotes:
  - int + float            -> float64
  - any + null             -> the non-null type
  - any string-ish either side -> large_string
  - bool + other           -> other

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

NAME_RE = re.compile(
    r"^(?P<datatype>[a-z0-9]+?)(?:_(?P<seasontype>po))?_(?P<season>\d{4})$"
)


def parse_name(stem: str) -> tuple[str, int, str] | None:
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
    with tarfile.open(archive, mode="r:xz") as tar:
        member = None
        for m in tar.getmembers():
            if m.name == expected_csv_name or m.name.endswith("/" + expected_csv_name):
                member = m
                break
        if member is None:
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

    return pd.read_csv(
        io.BytesIO(data),
        low_memory=False,
        encoding="latin-1",
    )


def convert_one(archive: Path) -> tuple[str, str]:
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
        df.to_parquet(out_path, engine="pyarrow", compression="snappy", index=False)
    except Exception as e:
        if out_path.exists():
            out_path.unlink()
        return ("failed", f"{archive.name} write: {e}")

    return ("converted", str(out_path))


# ---- Custom schema reconciler (the fix) ----

def _reconcile_type(a: pa.DataType, b: pa.DataType) -> pa.DataType:
    if pa.types.is_null(a):
        return b
    if pa.types.is_null(b):
        return a
    if a == b:
        return a

    a_int = pa.types.is_integer(a)
    b_int = pa.types.is_integer(b)
    a_float = pa.types.is_floating(a)
    b_float = pa.types.is_floating(b)

    if (a_int or a_float) and (b_int or b_float):
        return pa.float64()

    a_str = pa.types.is_string(a) or pa.types.is_large_string(a)
    b_str = pa.types.is_string(b) or pa.types.is_large_string(b)
    if a_str or b_str:
        return pa.large_string()

    if pa.types.is_boolean(a):
        return b
    if pa.types.is_boolean(b):
        return a

    return pa.large_string()


def build_unified_schema(schemas: list[pa.Schema]) -> pa.Schema:
    field_types: dict[str, pa.DataType] = {}
    field_order: list[str] = []
    for s in schemas:
        for f in s:
            if f.name not in field_types:
                field_types[f.name] = f.type
                field_order.append(f.name)
            else:
                field_types[f.name] = _reconcile_type(field_types[f.name], f.type)
    return pa.schema([pa.field(n, field_types[n]) for n in field_order])


def conform_table(table: pa.Table, target: pa.Schema) -> pa.Table:
    """Cast columns to target types, fill missing columns with nulls,
    reorder to match target."""
    cols: dict[str, pa.Array] = {}
    for f in target:
        if f.name in table.column_names:
            arr = table.column(f.name)
            if arr.type != f.type:
                arr = arr.cast(f.type, safe=False)
            cols[f.name] = arr
        else:
            cols[f.name] = pa.nulls(table.num_rows, type=f.type)
    return pa.table(cols, schema=target)


def build_merged(datatype: str) -> tuple[str, str]:
    folder = PER_SEASON_DIR / datatype
    if not folder.exists():
        return ("skipped", f"no per-season folder for {datatype}")

    season_files = sorted(folder.glob("*.parquet"))
    if not season_files:
        return ("skipped", f"no per-season files for {datatype}")

    out_path = merged_path(datatype)
    if out_path.exists():
        out_path.unlink()

    schemas = [pq.read_schema(f) for f in season_files]
    unified = build_unified_schema(schemas)

    with pq.ParquetWriter(out_path, unified, compression="snappy") as writer:
        for f in season_files:
            table = pq.read_table(f)
            table = conform_table(table, unified)
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

    counts = {"converted": 0, "skipped_existing": 0, "skipped": 0, "failed": 0}
    for arc in tqdm(archives, desc="per-season", unit="file"):
        status, msg = convert_one(arc)
        counts[status] = counts.get(status, 0) + 1
        if status == "failed":
            tqdm.write(f"  FAILED: {arc.name} - {msg}")

    print()
    print("Per-season summary:")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")

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
