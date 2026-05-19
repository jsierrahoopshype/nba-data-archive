"""
02_upload_to_releases.py

Uploads downloaded .tar.xz archives to GitHub Releases on
jsierrahoopshype/nba-data-archive.

Strategy: one Release per data type, tagged with the data type name.
This gives stable, predictable download URLs forever:

  https://github.com/jsierrahoopshype/nba-data-archive/releases/download/
      <data_type>/<filename>.tar.xz

For weekly refreshes, the same release tags get new assets uploaded over
the top via --clobber.

Resumable: queries each release for existing assets and skips files that
are already there. Re-uploading the whole 1 GB after a crash is not fun.

Prereqs:
  - gh CLI installed and authenticated as jsierrahoopshype
  - data/ folder populated by 01_download_archives.py

Usage:
  python scripts/02_upload_to_releases.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = "jsierrahoopshype/nba-data-archive"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# Per-data-type release configuration.
# tag is the release tag (and part of the download URL).
# title is the human-readable release name.
# notes is the description shown on the release page.
DATA_TYPES = {
    "shotdetail": {
        "title": "Shot Detail (1996/97 - 2025/26)",
        "notes": (
            "Every NBA shot since the 1996/97 season with X/Y court coordinates, "
            "shot type, distance, make/miss, period, and game context.\n\n"
            "**Coverage:** 1996/97 to 2025/26 regular season, 1996/97 to 2024/25 playoffs.\n\n"
            "Source: stats.nba.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
    "matchups": {
        "title": "Player Matchups (2017/18 - 2025/26)",
        "notes": (
            "Player-vs-player matchup data: possessions, partial possessions, "
            "and per-matchup box-score lines for every offensive-defensive pairing.\n\n"
            "**Coverage:** 2017/18 to 2025/26 regular season, 2017/18 to 2024/25 playoffs.\n\n"
            "Source: stats.nba.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
    "nbastats": {
        "title": "Play-by-Play - stats.nba.com (1996/97 - 2024/25)",
        "notes": (
            "Classic play-by-play from stats.nba.com: one row per event, with "
            "PLAYER1/2/3 IDs, event type, score, period, and game clock.\n\n"
            "**Coverage:** 1996/97 to 2024/25 regular season + playoffs. "
            "2025/26 not yet available upstream.\n\n"
            "Source: stats.nba.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
    "nbastatsv3": {
        "title": "Play-by-Play v3 - stats.nba.com (2020/21 - 2025/26)",
        "notes": (
            "Newer play-by-play schema from stats.nba.com with richer event "
            "metadata than the classic `nbastats` source.\n\n"
            "**Coverage:** 2020/21 to 2025/26 regular season, 2020/21 to 2024/25 playoffs.\n\n"
            "Source: stats.nba.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
    "pbpstats": {
        "title": "Possessions - pbpstats.com (2000/01 - 2024/25)",
        "notes": (
            "Possession-level data with possession start type (after made FG, "
            "after turnover, after offensive rebound, etc.), possession length, "
            "and outcome.\n\n"
            "**Coverage:** 2000/01 to 2024/25 regular season + playoffs. "
            "2025/26 not yet available upstream.\n\n"
            "Source: pbpstats.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
    "datanba": {
        "title": "Play-by-Play - data.nba.com (2016/17 - 2024/25)",
        "notes": (
            "Play-by-play with on-court XY coordinates for each action, "
            "useful for spatial analysis beyond just shots.\n\n"
            "**Coverage:** 2016/17 to 2024/25 regular season + playoffs. "
            "2025/26 not yet available upstream.\n\n"
            "Source: data.nba.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
    "cdnnba": {
        "title": "Play-by-Play - cdn.nba.com (2016/17 - 2025/26)",
        "notes": (
            "Lightweight play-by-play feed from cdn.nba.com, smaller per-game "
            "footprint than the other PBP sources.\n\n"
            "**Coverage:** 2016/17 to 2025/26 regular season, 2016/17 to 2024/25 playoffs.\n\n"
            "Source: cdn.nba.com (via shufinskiy/nba_data, Apache 2.0)."
        ),
    },
}


def gh(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run gh CLI. capture=False streams output to terminal."""
    if capture:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8"
        )
    return subprocess.run(["gh", *args], text=True, encoding="utf-8")


def check_gh_ready() -> None:
    r = subprocess.run(["gh", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: gh CLI is not installed.", file=sys.stderr)
        print("Install with: winget install --id GitHub.cli", file=sys.stderr)
        sys.exit(1)

    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: gh CLI is not authenticated.", file=sys.stderr)
        print("Authenticate with: gh auth login", file=sys.stderr)
        sys.exit(1)


def release_exists(tag: str) -> bool:
    r = gh("release", "view", tag, "--repo", REPO)
    return r.returncode == 0


def ensure_release(tag: str, title: str, notes: str) -> None:
    if release_exists(tag):
        return
    print(f"Creating release: {tag}")
    r = gh(
        "release",
        "create",
        tag,
        "--repo",
        REPO,
        "--title",
        title,
        "--notes",
        notes,
    )
    if r.returncode != 0:
        print(f"  Failed to create release {tag}: {r.stderr}", file=sys.stderr)
        sys.exit(1)


def get_existing_assets(tag: str) -> set[str]:
    r = gh(
        "release",
        "view",
        tag,
        "--repo",
        REPO,
        "--json",
        "assets",
        "--jq",
        ".assets[].name",
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    return set(line.strip() for line in r.stdout.strip().splitlines() if line.strip())


def upload_batch(tag: str, files: list[Path]) -> bool:
    """Upload a list of files to a release. Streams gh output to terminal."""
    if not files:
        return True
    args = ["release", "upload", tag, "--repo", REPO, *[str(f) for f in files]]
    r = gh(*args, capture=False)
    return r.returncode == 0


def main() -> int:
    check_gh_ready()

    if not DATA_DIR.exists():
        print(f"ERROR: data folder not found: {DATA_DIR}", file=sys.stderr)
        print("Run 01_download_archives.py first.", file=sys.stderr)
        return 1

    print(f"Repo:        {REPO}")
    print(f"Data folder: {DATA_DIR}")
    print()

    totals = {"uploaded": 0, "skipped": 0, "missing": 0}

    for tag, meta in DATA_TYPES.items():
        files = sorted(DATA_DIR.glob(f"{tag}_*.tar.xz"))
        if not files:
            print(f"[{tag}] no files matching pattern, skipping release entirely")
            continue

        print(f"\n=== {tag} ({len(files)} candidate files) ===")
        ensure_release(tag, meta["title"], meta["notes"])

        existing = get_existing_assets(tag)
        to_upload = [f for f in files if f.name not in existing]
        skipped = len(files) - len(to_upload)

        print(f"  already on release: {skipped}")
        print(f"  to upload:          {len(to_upload)}")

        if not to_upload:
            totals["skipped"] += skipped
            continue

        # Upload in batches of 25 so a single failure doesn't lose much progress.
        BATCH = 25
        uploaded_here = 0
        for i in range(0, len(to_upload), BATCH):
            batch = to_upload[i : i + BATCH]
            print(
                f"  uploading batch {i // BATCH + 1}/"
                f"{(len(to_upload) + BATCH - 1) // BATCH} "
                f"({len(batch)} files)..."
            )
            ok = upload_batch(tag, batch)
            if not ok:
                print(
                    f"  WARN: batch upload to {tag} reported failure; "
                    "rerun the script to retry remaining files",
                    file=sys.stderr,
                )
                break
            uploaded_here += len(batch)

        totals["uploaded"] += uploaded_here
        totals["skipped"] += skipped

    print()
    print("=== Done ===")
    for k, v in totals.items():
        print(f"  {k:20s} {v}")
    print()
    print(
        f"Releases page: https://github.com/{REPO}/releases"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
