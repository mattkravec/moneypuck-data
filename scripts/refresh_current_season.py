#!/usr/bin/env python3
"""
refresh_current_season.py — nightly in-season refresh for moneypuck-data.

Runs in GitHub Actions (moneypuck.com and peter-tanner.com are NOT reachable
from the Claude sandbox, so this cannot run inside a moneypuck-nhl skill
session — it has to run here, in the data repo, on its own schedule).

What it does, in order:
  1. Backs up the CURRENT (pre-refresh) copies of the six mid-season files
     from the `data-v2` release into the `data-v2-backups` release, under
     dated names, then prunes backup assets older than BACKUP_RETENTION_DAYS.
  2. Downloads fresh current-season CSVs straight from MoneyPuck.
  3. Converts them to Parquet using the same conventions as the original
     historical conversion (row_group_size for pushdown, lineId cast to str
     to dodge the int64 overflow).
  4. Uploads the fresh Parquet files to `data-v2`, overwriting in place
     (--clobber) so the release URLs never change and MPDATA_RELEASE_BASE
     doesn't need to be touched.

Historical multi-season files (skaters_2008_to_2024.parquet etc.) are never
touched here — they're closed once a season ends.

Requires: pandas, pyarrow, requests, and the `gh` CLI authenticated via
GITHUB_TOKEN (GitHub Actions provides this automatically).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
REPO = os.environ.get("MPDATA_REPO", "mattkravec/moneypuck-data")
LIVE_TAG = os.environ.get("MPDATA_LIVE_TAG", "data-v2")
BACKUP_TAG = os.environ.get("MPDATA_BACKUP_TAG", "data-v2-backups")
BACKUP_RETENTION_DAYS = int(os.environ.get("MPDATA_BACKUP_RETENTION_DAYS", "7"))

# season is labeled by its START year -- 2025 == the 2025-26 season.
# Bump this once a year when the new season starts; it does NOT change
# mid-season.
CURRENT_SEASON = int(os.environ.get("MPDATA_CURRENT_SEASON", "2025"))

WORKDIR = "mp_refresh_tmp"

# The six files whose *content* changes mid-season. Filenames here are the
# release asset names (what mpdata.py's CURRENT_SEASON_FILES expects).
CURRENT_SEASON_FILES = [
    "skaters_2025.parquet",
    "goalies_2025.parquet",
    "lines_2025.parquet",
    "teams_2025.parquet",
    "shots_2025.parquet",
    "all_teams_2008_to2025.parquet",
]

# Where each one comes from on MoneyPuck. seasonSummary files are plain CSV;
# shots ships zipped; the game log is a single cumulative CSV covering every
# season (not just the current one).
SOURCES = {
    "skaters_2025.parquet": {
        "url": f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{CURRENT_SEASON}/regular/skaters.csv",
        "kind": "csv",
    },
    "goalies_2025.parquet": {
        "url": f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{CURRENT_SEASON}/regular/goalies.csv",
        "kind": "csv",
    },
    "lines_2025.parquet": {
        "url": f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{CURRENT_SEASON}/regular/lines.csv",
        "kind": "csv",
    },
    "teams_2025.parquet": {
        "url": f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{CURRENT_SEASON}/regular/teams.csv",
        "kind": "csv",
    },
    "shots_2025.parquet": {
        "url": f"https://peter-tanner.com/moneypuck/downloads/shots_{CURRENT_SEASON}.zip",
        "kind": "zip",
    },
    "all_teams_2008_to2025.parquet": {
        "url": "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv",
        "kind": "csv",
    },
}

# Columns known to overflow int64 when MoneyPuck concatenates player IDs.
# Cast to str wherever present, matching the original historical conversion.
_OVERSIZED_ID_COLS = ["lineId"]


# --------------------------------------------------------------------------- #
# Fetch + convert
# --------------------------------------------------------------------------- #
def _fetch_csv(url: str) -> pd.DataFrame:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), low_memory=False)


def _fetch_zipped_csv(url: str) -> pd.DataFrame:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise FileNotFoundError(f"No CSV found inside zip from {url}")
        with zf.open(names[0]) as f:
            return pd.read_csv(f, low_memory=False)


def fetch_and_convert(dest_name: str, spec: dict) -> str:
    print(f"  fetching {dest_name} <- {spec['url']}")
    if spec["kind"] == "zip":
        df = _fetch_zipped_csv(spec["url"])
    else:
        df = _fetch_csv(spec["url"])

    for col in _OVERSIZED_ID_COLS:
        if col in df.columns:
            df[col] = df[col].astype(str)

    out_path = os.path.join(WORKDIR, dest_name)
    row_group_size = 100_000 if len(df) > 100_000 else None
    kwargs = {"index": False}
    if row_group_size:
        kwargs["row_group_size"] = row_group_size
    df.to_parquet(out_path, **kwargs)
    print(f"    -> {out_path} ({len(df):,} rows, "
          f"{os.path.getsize(out_path) / 1e6:.1f} MB)")
    return out_path


# --------------------------------------------------------------------------- #
# gh CLI helpers
# --------------------------------------------------------------------------- #
def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], check=check,
                           capture_output=True, text=True)


def ensure_backup_release_exists() -> None:
    r = _gh("release", "view", BACKUP_TAG, "-R", REPO, check=False)
    if r.returncode != 0:
        print(f"creating backup release {BACKUP_TAG}")
        _gh("release", "create", BACKUP_TAG, "-R", REPO,
            "--title", "Rolling backups (auto-pruned)",
            "--notes", f"Pre-refresh snapshots of {LIVE_TAG}'s mid-season "
                       f"files, kept for {BACKUP_RETENTION_DAYS} days.",
            "--prerelease")


def backup_current_assets() -> None:
    """Download today's pre-refresh copies from the live release and
    re-upload them into the backup release under a dated name, then prune
    anything past the retention window."""
    ensure_backup_release_exists()
    today = date.today().isoformat()
    dated_paths = []
    for name in CURRENT_SEASON_FILES:
        url = (f"https://github.com/{REPO}/releases/download/{LIVE_TAG}/{name}")
        r = requests.get(url, timeout=300, allow_redirects=True)
        if r.status_code != 200:
            print(f"  skip backup for {name}: not present in {LIVE_TAG} yet "
                  f"(HTTP {r.status_code}) -- fine on first run")
            continue
        stem, ext = name.rsplit(".", 1)
        dated_name = f"{stem}__{today}.{ext}"
        dated_path = os.path.join(WORKDIR, dated_name)
        with open(dated_path, "wb") as f:
            f.write(r.content)
        dated_paths.append(dated_path)

    if dated_paths:
        print(f"  uploading {len(dated_paths)} dated backups to {BACKUP_TAG}")
        _gh("release", "upload", BACKUP_TAG, *dated_paths,
            "-R", REPO, "--clobber")

    prune_old_backups()


def prune_old_backups() -> None:
    r = _gh("release", "view", BACKUP_TAG, "-R", REPO,
             "--json", "assets", check=False)
    if r.returncode != 0:
        return
    import json
    assets = json.loads(r.stdout).get("assets", [])
    cutoff = datetime.now().date() - timedelta(days=BACKUP_RETENTION_DAYS)
    for a in assets:
        name = a["name"]
        # dated names look like "skaters_2025__2026-08-01.parquet"
        try:
            date_part = name.rsplit("__", 1)[1].rsplit(".", 1)[0]
            asset_date = date.fromisoformat(date_part)
        except (IndexError, ValueError):
            continue
        if asset_date < cutoff:
            print(f"  pruning old backup: {name} ({asset_date})")
            _gh("release", "delete-asset", BACKUP_TAG, name,
                "-R", REPO, "--yes", check=False)


def upload_live(paths: list[str]) -> None:
    print(f"uploading {len(paths)} refreshed files to {LIVE_TAG}")
    _gh("release", "upload", LIVE_TAG, *paths, "-R", REPO, "--clobber")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    os.makedirs(WORKDIR, exist_ok=True)

    print(f"[1/3] backing up pre-refresh assets to {BACKUP_TAG} "
          f"(retention: {BACKUP_RETENTION_DAYS} days)")
    backup_current_assets()

    print("[2/3] fetching + converting current-season files")
    fresh_paths = []
    failures = []
    for name, spec in SOURCES.items():
        try:
            fresh_paths.append(fetch_and_convert(name, spec))
        except Exception as e:
            print(f"  FAILED {name}: {e}", file=sys.stderr)
            failures.append(name)

    if not fresh_paths:
        print("Nothing fetched successfully -- aborting without touching "
              f"{LIVE_TAG}.", file=sys.stderr)
        sys.exit(1)

    print(f"[3/3] uploading {len(fresh_paths)}/{len(SOURCES)} files")
    upload_live(fresh_paths)

    if failures:
        print(f"Completed with {len(failures)} failure(s): {failures}",
              file=sys.stderr)
        sys.exit(1)
    print("Refresh complete.")


if __name__ == "__main__":
    main()
