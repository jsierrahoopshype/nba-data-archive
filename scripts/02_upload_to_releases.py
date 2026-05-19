"""
02_upload_to_releases.py

Uploads downloaded .tar.xz archives to GitHub Releases on
jsierrahoopshype/nba-data-archive.

One Release per data type, tagged with the data type name. Stable URL pattern:
  https://github.com/jsierrahoopshype/nba-data-archive/releases/download/
      <data_type>/<filename>.tar.xz

Resumable: queries each release for existing assets and skips files that
are already there.

--clobber-current-season: re-uploads (overwrites) any file whose season is
>= --current-season-floor. Useful for the cron, since active-season files
upstream can be re-published without changing names.

Prereqs:
  - gh CLI installed and authenticated
  - data/ folder populated by 01_download_archives.py

Usage:
  python scripts/02_upload_to_releases.py
  python scripts/02_upload_to_releases.py --clobber-current-season
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = "jsierrahoopshype/nba-data-archive"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

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


def season_of(filename: str) -> int | None:
    stem = filename.removesuffix(".tar.xz")
    for p in reversed(stem.split("_")):
        if p.isdigit() and len(p) == 4:
            return int(p)
    return None


def gh(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    if capture:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8"
        )
    return subprocess.run(["gh", *args], text=True, encoding="utf-8")


def check_gh_ready() -> None:
    r = subprocess.run(["gh", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)

    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: gh CLI is not authenticated.", file=sys.stderr)
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


def upload_batch(tag: str, files: list[Path], clobber: bool) -> bool:
    if not files:
        return True
    args = ["release", "upload", tag, "--repo", REPO]
    if clobber:
        args.append("--clobber")
    args.extend(str(f) for f in files)
    r = gh(*args, capture=False)
    return r.returncode == 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clobber-current-season", action="store_true")
    parser.add_argument("--current-season-floor", type=int, default=2025)
    args = parser.parse_args(argv)

    check_gh_ready()

    if not DATA_DIR.exists():
        print(f"ERROR: data folder not found: {DATA_DIR}", file=sys.stderr)
        return 1

    print(f"Repo:        {REPO}")
    print(f"Data folder: {DATA_DIR}")
    if args.clobber_current_season:
        print(f"Clobbering files for seasons >= {args.current_season_floor}")
    print()

    totals = {"uploaded": 0, "skipped": 0, "clobbered": 0}

    for tag, meta in DATA_TYPES.items():
        files = sorted(DATA_DIR.glob(f"{tag}_*.tar.xz"))
        if not files:
            print(f"[{tag}] no files matching pattern, skipping")
            continue

        print(f"\n=== {tag} ({len(files)} candidate files) ===")
        ensure_release(tag, meta["title"], meta["notes"])

        existing = get_existing_assets(tag)

        # Split: fresh uploads vs clobber re-uploads vs skip.
        fresh: list[Path] = []
        clobber: list[Path] = []
        skipped = 0

        for f in files:
            if f.name not in existing:
                fresh.append(f)
                continue
            # Already on release.
            if args.clobber_current_season:
                s = season_of(f.name)
                if s is not None and s >= args.current_season_floor:
                    clobber.append(f)
                    continue
            skipped += 1

        print(f"  already on release (skip):   {skipped}")
        print(f"  new (to upload):             {len(fresh)}")
        print(f"  clobber (current season):    {len(clobber)}")

        # Fresh uploads, batched.
        BATCH = 25
        if fresh:
            for i in range(0, len(fresh), BATCH):
                batch = fresh[i : i + BATCH]
                print(
                    f"  fresh batch {i // BATCH + 1}/"
                    f"{(len(fresh) + BATCH - 1) // BATCH} ({len(batch)} files)..."
                )
                if not upload_batch(tag, batch, clobber=False):
                    print(
                        f"  WARN: fresh batch failed on {tag}; rerun to retry",
                        file=sys.stderr,
                    )
                    break
            totals["uploaded"] += len(fresh)

        # Clobbers, batched.
        if clobber:
            for i in range(0, len(clobber), BATCH):
                batch = clobber[i : i + BATCH]
                print(
                    f"  clobber batch {i // BATCH + 1}/"
                    f"{(len(clobber) + BATCH - 1) // BATCH} ({len(batch)} files)..."
                )
                if not upload_batch(tag, batch, clobber=True):
                    print(
                        f"  WARN: clobber batch failed on {tag}; rerun to retry",
                        file=sys.stderr,
                    )
                    break
            totals["clobbered"] += len(clobber)

        totals["skipped"] += skipped

    print()
    print("=== Done ===")
    for k, v in totals.items():
        print(f"  {k:20s} {v}")
    print()
    print(f"Releases page: https://github.com/{REPO}/releases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
