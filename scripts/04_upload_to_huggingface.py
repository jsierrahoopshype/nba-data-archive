"""
04_upload_to_huggingface.py

Uploads the parquet/ folder produced by 03_convert_to_parquet.py to the
HuggingFace public dataset `cdechoch/nba-data-archive`.

What lands on HF:
  per_season/<datatype>/<season>.parquet        (per-season-per-type splits)
  per_season/<datatype>/po_<season>.parquet
  merged/<datatype>.parquet                     (one big file per datatype)
  README.md                                     (dataset card)

The dataset is public, Apache-2.0, with credit to the upstream source
(shufinskiy/nba_data).

Resumable: HfApi.upload_large_folder handles retries and partial uploads
gracefully. Re-run freely.

Prereq:
  hf auth login   (with a Write token, account cdechoch)

Usage:
  python scripts/04_upload_to_huggingface.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

from huggingface_hub import HfApi


REPO_ROOT = Path(__file__).resolve().parent.parent
PARQUET_DIR = REPO_ROOT / "parquet"

HF_REPO_ID = "cdechoch/nba-data-archive"
HF_REPO_TYPE = "dataset"

# Built once on the local parquet/ folder before upload so the README is
# part of the same atomic upload as the data.
DATASET_CARD = dedent(
    """\
    ---
    license: apache-2.0
    pretty_name: NBA Data Archive (1996-2026)
    tags:
      - basketball
      - nba
      - sports
      - play-by-play
      - shot-charts
    size_categories:
      - 10M<n<100M
    ---

    # NBA Data Archive

    Parquet mirror of NBA play-by-play, shot detail, and player-matchup data
    from the 1996/97 season through 2025/26.

    Built and maintained as the data layer for [HoopsMatic.com](https://hoopsmatic.com)
    analytics tools and [HoopsHype](https://hoopshype.com) editorial automation.
    Public so other researchers and tinkerers don't have to rebuild the same
    pipeline.

    ## Layout

    Two complementary layouts; pick whichever fits your query pattern.

    ### `per_season/`

    One Parquet file per (data type, season, season type). Use this when you
    only need a slice — a single season, a recent few years, regular season
    only, etc.

    ```
    per_season/shotdetail/2024.parquet     # 2024/25 regular season shots
    per_season/shotdetail/po_2023.parquet  # 2023/24 playoff shots
    per_season/matchups/2017.parquet
    ...
    ```

    ### `merged/`

    One Parquet file per data type, all seasons + season types concatenated.
    Use this when you want to sweep the whole history at once (career-long
    shot charts, all-time leaderboards, multi-season trend analysis).

    ```
    merged/shotdetail.parquet
    merged/matchups.parquet
    merged/nbastats.parquet
    merged/nbastatsv3.parquet
    merged/pbpstats.parquet
    merged/datanba.parquet
    merged/cdnnba.parquet
    ```

    Every row in both layouts carries two provenance columns:

    - `_season` — int, e.g. `2024` for the 2024/25 season
    - `_season_type` — `"rg"` (regular) or `"po"` (playoffs)

    ## Data types

    | Type | Coverage | Description |
    |---|---|---|
    | `shotdetail` | 1996/97-2025/26 | Every shot with X/Y court coordinates, distance, make/miss, period, game context |
    | `matchups` | 2017/18-2025/26 | Player-vs-player matchup possessions and box-score lines |
    | `nbastats` | 1996/97-2024/25 | Classic play-by-play from stats.nba.com |
    | `nbastatsv3` | 2020/21-2025/26 | Newer play-by-play schema |
    | `pbpstats` | 2000/01-2024/25 | Possession-level data with start-type tags |
    | `datanba` | 2016/17-2024/25 | Play-by-play with on-court action coordinates |
    | `cdnnba` | 2016/17-2025/26 | Lightweight play-by-play from cdn.nba.com |

    Playoff coverage runs through 2024/25; 2025/26 playoffs don't exist yet.
    Some sources (`nbastats`, `pbpstats`, `datanba`) don't yet have 2025/26
    backfilled upstream.

    ## Quick start

    ```python
    from datasets import load_dataset

    # Load all shots from a single season
    ds = load_dataset(
        "cdechoch/nba-data-archive",
        data_files="per_season/shotdetail/2024.parquet",
        split="train",
    )

    # Or load the full career history of every shot ever
    ds = load_dataset(
        "cdechoch/nba-data-archive",
        data_files="merged/shotdetail.parquet",
        split="train",
    )
    ```

    Or skip the `datasets` library and read Parquet directly with `pandas` or
    `pyarrow` — every file is standard, snappy-compressed Parquet.

    ## Source and license

    Underlying data collected and published by Vladislav Shufinskiy at
    [shufinskiy/nba_data](https://github.com/shufinskiy/nba_data) under the
    Apache License 2.0. This dataset is the same data converted to Parquet
    and redistributed under the same license.

    Original upstream sources, in turn: `stats.nba.com`, `data.nba.com`,
    `cdn.nba.com`, `pbpstats.com`.

    Statistics themselves are not copyrightable
    (*Feist Publications v. Rural Telephone Service*, 1991). Player names,
    team names, and game data are factual information used in a nominative
    reference capacity. No NBA trademarks, logos, or proprietary content are
    included.

    ## Raw archives

    The original `.tar.xz` files are mirrored at
    [jsierrahoopshype/nba-data-archive](https://github.com/jsierrahoopshype/nba-data-archive)
    on GitHub Releases, one release per data type, in case anyone prefers to
    work from the CSVs.
    """
)


def main() -> int:
    if not PARQUET_DIR.exists():
        print(f"ERROR: {PARQUET_DIR} not found.", file=sys.stderr)
        print("Run 03_convert_to_parquet.py first.", file=sys.stderr)
        return 1

    # Write the dataset card locally so it ships with the upload.
    (PARQUET_DIR / "README.md").write_text(DATASET_CARD, encoding="utf-8")

    api = HfApi()

    # Create-if-not-exists. Idempotent, public dataset.
    print(f"Ensuring HF dataset repo exists: {HF_REPO_ID}")
    api.create_repo(
        repo_id=HF_REPO_ID,
        repo_type=HF_REPO_TYPE,
        exist_ok=True,
        private=False,
    )

    print(f"Uploading folder: {PARQUET_DIR}")
    print(f"  -> {HF_REPO_ID}")
    print("This streams in the background with retries; sit tight.")
    print()

    # upload_large_folder is the resilient, resumable bulk uploader.
    api.upload_large_folder(
        folder_path=str(PARQUET_DIR),
        repo_id=HF_REPO_ID,
        repo_type=HF_REPO_TYPE,
        # We could ignore .DS_Store / Thumbs.db but the folder won't have any.
    )

    print()
    print("Done.")
    print(f"Dataset: https://huggingface.co/datasets/{HF_REPO_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
