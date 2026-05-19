"""
01_download_archives.py

Downloads NBA archive files from shufinskiy/nba_data into local data/ folder.

Scope: NBA only (no WNBA), all 7 data types, regular season + playoffs, all
available seasons (1996/97 onward, where present per source).

Smart resume:
  - Missing files -> downloaded.
  - Corrupt local files -> redownloaded.
  - Valid local files -> HEAD upstream and compare Content-Length. If sizes
    differ, redownload (upstream re-published). If sizes match, skip.
  - --force-current-season redownloads any file whose season is >= the
    --current-season-floor (default 2025 = the 2025/26 season). Useful for
    weekly cron, since active-season files can be re-uploaded with identical
    byte counts but different content.

Usage:
  python scripts/01_download_archives.py
  python scripts/01_download_archives.py --force-current-season
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import requests
from tqdm import tqdm


LIST_URL = "https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt"

DATA_TYPES = (
    "shotdetail",
    "matchups",
    "nbastats",
    "nbastatsv3",
    "pbpstats",
    "datanba",
    "cdnnba",
)

SEASON_FLOOR = 1996
SEASON_CEIL = 2025

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "_manifest.json"

CHUNK_SIZE = 1024 * 64
REQUEST_TIMEOUT = 60
INTER_FILE_DELAY = 0.5
USER_AGENT = "nba-data-archive/0.1 (https://github.com/jsierrahoopshype/nba-data-archive)"


def fetch_upstream_list() -> dict[str, str]:
    req = Request(LIST_URL, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Upstream list returned HTTP {resp.status}")
        text = resp.read().decode("utf-8").strip()

    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        name, url = line.split("=", 1)
        pairs[name.strip()] = url.strip()
    return pairs


def build_wanted_names() -> list[str]:
    names: list[str] = []
    for dtype in DATA_TYPES:
        for season in range(SEASON_FLOOR, SEASON_CEIL + 1):
            names.append(f"{dtype}_{season}")
        for season in range(SEASON_FLOOR, SEASON_CEIL + 1):
            names.append(f"{dtype}_po_{season}")
    return names


def season_of(name: str) -> int | None:
    for p in reversed(name.split("_")):
        if p.isdigit() and len(p) == 4:
            return int(p)
    return None


def is_valid_tar_xz(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with tarfile.open(path, mode="r:xz") as tar:
            tar.getmembers()
        return True
    except (tarfile.TarError, EOFError, OSError):
        return False


def remote_size(url: str) -> int | None:
    try:
        r = requests.head(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return None
        cl = r.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except (requests.RequestException, ValueError):
        return None


def download_file(name: str, url: str, dest: Path, force: bool) -> tuple[str, int, str]:
    if not force and dest.exists() and is_valid_tar_xz(dest):
        local_size = dest.stat().st_size
        remote = remote_size(url)
        if remote is None:
            return ("skipped_existing", local_size, "valid locally; remote size unknown")
        if remote == local_size:
            return ("skipped_existing", local_size, "valid locally; size matches remote")

    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()

    try:
        with requests.get(
            url,
            stream=True,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as r:
            if r.status_code == 404:
                return ("skipped_missing_upstream", 0, "404 not found upstream")
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))

            with open(tmp, "wb") as fh, tqdm(
                total=total if total else None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=name,
                leave=False,
            ) as bar:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    bar.update(len(chunk))

    except (requests.RequestException, HTTPError, URLError, OSError) as e:
        if tmp.exists():
            tmp.unlink()
        return ("failed", 0, f"download error: {e}")

    if not is_valid_tar_xz(tmp):
        size = tmp.stat().st_size if tmp.exists() else 0
        if tmp.exists():
            tmp.unlink()
        return ("failed", size, "downloaded but failed tar.xz validation")

    tmp.replace(dest)
    return ("downloaded", dest.stat().st_size, "ok")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-current-season", action="store_true")
    parser.add_argument("--current-season-floor", type=int, default=2025)
    args = parser.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Repo root:    {REPO_ROOT}")
    print(f"Data folder:  {DATA_DIR}")
    print(f"Manifest:     {MANIFEST_PATH}")
    if args.force_current_season:
        print(f"Force-refresh seasons >= {args.current_season_floor}")
    print()

    print("Fetching upstream file list...")
    upstream = fetch_upstream_list()
    print(f"Upstream advertises {len(upstream)} files.")

    wanted = build_wanted_names()
    in_scope = [n for n in wanted if n in upstream]
    out_of_scope = [n for n in wanted if n not in upstream]
    print(f"In scope for download (present upstream): {len(in_scope)}")
    print(f"Out of scope (not in upstream list):      {len(out_of_scope)}")
    print()

    results: dict[str, dict] = {}
    for name in out_of_scope:
        results[name] = {
            "status": "skipped_missing_upstream",
            "url": None,
            "size_bytes": 0,
            "message": "not present in upstream list_data.txt",
        }

    print("Downloading...")
    for name in tqdm(in_scope, desc="overall", unit="file"):
        url = upstream[name]
        dest = DATA_DIR / f"{name}.tar.xz"

        force = False
        if args.force_current_season:
            s = season_of(name)
            if s is not None and s >= args.current_season_floor:
                force = True

        status, size, msg = download_file(name, url, dest, force=force)
        results[name] = {
            "status": status,
            "url": url,
            "size_bytes": size,
            "message": msg,
        }
        if status == "failed":
            tqdm.write(f"  FAILED: {name} - {msg}")
        if status == "downloaded":
            time.sleep(INTER_FILE_DELAY)

    manifest = {
        "generated_at_unix": int(time.time()),
        "upstream_list_url": LIST_URL,
        "upstream_file_count": len(upstream),
        "wanted_count": len(wanted),
        "in_scope_count": len(in_scope),
        "out_of_scope_count": len(out_of_scope),
        "force_current_season": args.force_current_season,
        "current_season_floor": args.current_season_floor,
        "files": results,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))

    counts: dict[str, int] = {}
    total_bytes = 0
    for entry in results.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        total_bytes += entry["size_bytes"]

    print()
    print("Done.")
    for status, n in sorted(counts.items()):
        print(f"  {status:30s} {n}")
    print(f"  total bytes on disk             {total_bytes:,}")
    print()
    print(f"Manifest written to: {MANIFEST_PATH}")

    failed = counts.get("failed", 0)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
