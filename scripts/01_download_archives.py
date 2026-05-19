"""
01_download_archives.py

Downloads NBA archive files from shufinskiy/nba_data into local data/ folder.

Scope: NBA only (no WNBA), all 7 data types, regular season + playoffs, all
available seasons (1996/97 onward, where present per source).

Resumable: rerun freely. Files already on disk that pass tar.xz validation
are skipped. Partial or corrupt files are redownloaded.

Output:
  data/{name}.tar.xz                   one archive per data type per season
  data/_manifest.json                  ledger of what was downloaded

Usage:
  python scripts/01_download_archives.py
"""

from __future__ import annotations

import json
import sys
import tarfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import requests
from tqdm import tqdm


# ---------- Configuration ----------

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

# Earliest season per data type (per Shufinskiy README). The script asks the
# upstream list_data.txt what actually exists, so these are just the floor
# for what we expect to find. Anything not present upstream is logged as
# "skipped (not in upstream list)".
SEASON_FLOOR = 1996
SEASON_CEIL = 2025  # 2025 = the 2025/26 season

SEASON_TYPES = ("rg", "po")  # regular season + playoffs

# Where to put files (relative to repo root, which is the parent of scripts/)
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "_manifest.json"

# Network behavior
CHUNK_SIZE = 1024 * 64       # 64 KB chunks for download progress
REQUEST_TIMEOUT = 60         # seconds per HTTP call
INTER_FILE_DELAY = 0.5       # seconds between downloads, be polite
USER_AGENT = "nba-data-archive/0.1 (https://github.com/jsierrahoopshype/nba-data-archive)"


# ---------- Helpers ----------

def fetch_upstream_list() -> dict[str, str]:
    """
    Pulls list_data.txt from Shufinskiy's repo and returns {name: url}.
    Names look like 'shotdetail_2024' or 'pbpstats_po_2023' (no '.tar.xz').
    """
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
    """
    Builds the canonical list of names we want, in a sensible download order:
    by data type, regular season first (oldest to newest), then playoffs.
    """
    names: list[str] = []
    for dtype in DATA_TYPES:
        for season in range(SEASON_FLOOR, SEASON_CEIL + 1):
            names.append(f"{dtype}_{season}")
        for season in range(SEASON_FLOOR, SEASON_CEIL + 1):
            names.append(f"{dtype}_po_{season}")
    return names


def is_valid_tar_xz(path: Path) -> bool:
    """Returns True if the file opens cleanly as a tar.xz archive."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with tarfile.open(path, mode="r:xz") as tar:
            # Touch the member list so we actually parse the index.
            tar.getmembers()
        return True
    except (tarfile.TarError, EOFError, OSError):
        return False


def download_file(name: str, url: str, dest: Path) -> tuple[str, int, str]:
    """
    Streams a file to dest, with a per-file progress bar.
    Returns (status, size_bytes, message). Status is one of:
      'downloaded' | 'skipped_existing' | 'skipped_missing_upstream' | 'failed'
    """
    if dest.exists() and is_valid_tar_xz(dest):
        return ("skipped_existing", dest.stat().st_size, "already on disk")

    # Use a tmp path so an interrupted download never leaves a half file
    # that looks legitimate but isn't.
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

    # Validate before promoting from .part to final name.
    if not is_valid_tar_xz(tmp):
        size = tmp.stat().st_size if tmp.exists() else 0
        if tmp.exists():
            tmp.unlink()
        return ("failed", size, "downloaded but failed tar.xz validation")

    tmp.replace(dest)
    return ("downloaded", dest.stat().st_size, "ok")


# ---------- Main ----------

def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Repo root:    {REPO_ROOT}")
    print(f"Data folder:  {DATA_DIR}")
    print(f"Manifest:     {MANIFEST_PATH}")
    print()

    print("Fetching upstream file list...")
    upstream = fetch_upstream_list()
    print(f"Upstream advertises {len(upstream)} files.")

    wanted = build_wanted_names()
    # Only attempt names that exist upstream; record the rest as skipped-missing.
    in_scope = [n for n in wanted if n in upstream]
    out_of_scope = [n for n in wanted if n not in upstream]
    print(f"In scope for download (present upstream): {len(in_scope)}")
    print(f"Out of scope (not in upstream list):      {len(out_of_scope)}")
    print()

    results: dict[str, dict] = {}

    # Pre-record the misses so they show up in the manifest cleanly.
    for name in out_of_scope:
        results[name] = {
            "status": "skipped_missing_upstream",
            "url": None,
            "size_bytes": 0,
            "message": "not present in upstream list_data.txt",
        }

    # Now do the actual downloads with an overall progress bar.
    print("Downloading...")
    for i, name in enumerate(tqdm(in_scope, desc="overall", unit="file"), start=1):
        url = upstream[name]
        dest = DATA_DIR / f"{name}.tar.xz"
        status, size, msg = download_file(name, url, dest)
        results[name] = {
            "status": status,
            "url": url,
            "size_bytes": size,
            "message": msg,
        }
        if status == "failed":
            tqdm.write(f"  FAILED: {name} — {msg}")
        # be polite
        if status == "downloaded":
            time.sleep(INTER_FILE_DELAY)

    # Write manifest.
    manifest = {
        "generated_at_unix": int(time.time()),
        "upstream_list_url": LIST_URL,
        "upstream_file_count": len(upstream),
        "wanted_count": len(wanted),
        "in_scope_count": len(in_scope),
        "out_of_scope_count": len(out_of_scope),
        "files": results,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))

    # Summary.
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
    sys.exit(main())
