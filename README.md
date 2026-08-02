# moneypuck-data

Parquet mirror of the [MoneyPuck.com](https://moneypuck.com/data.htm) NHL data
dump, hosted as GitHub Release assets and refreshed nightly during the season.

This repo holds almost no files — the data lives in
[Releases](../../releases). What's committed here is the tooling that keeps
those release assets current.

> Data is from MoneyPuck.com and is free for non-commercial use. Credit
> MoneyPuck.com anywhere you publish anything derived from it.

## Why this exists

The [`moneypuck-nhl`](https://github.com/mattkravec) Claude skill needs to pull
its own data in a sandbox that can't reach `moneypuck.com`, Google Drive, or
Git LFS. GitHub Release assets resolve to `release-assets.githubusercontent.com`,
which *is* reachable, and allow 2 GB per file without bloating the repo.

Storing Parquet rather than raw CSV buys roughly 10x compression (the 120 MB
`all_teams` game log lands around 23 MB), preserves dtypes, and makes subsetted
reads about 14x faster via column pushdown — a 4-column read of a shots file is
~0.55 s against ~7.5 s for all 137 columns.

## What's hosted

**`data-v2`** — the live tag every consumer points at. ~380 MB across 28 assets:

| Asset | Coverage | Refreshed |
| --- | --- | --- |
| `shots_2007.parquet` … `shots_2025.parquet` | one file per season, per shot attempt | current season only |
| `skaters_2008_to_2024.parquet`, `skaters_2025.parquet` | per-skater season totals | current season only |
| `goalies_2008_to_2024.parquet`, `goalies_2025.parquet` | per-goalie season totals | current season only |
| `lines_2008_to_2024.parquet`, `lines_2025.parquet` | line combinations (5on5 only) | current season only |
| `teams_2008_to_2024.parquet`, `teams_2025.parquet` | per-team season totals | current season only |
| `all_teams_2008_to2025.parquet` | game-by-game team logs, all seasons | nightly |

Seasons are labeled by **start year**: `2025` is the 2025-26 season.

Asset names are stable and the refresh overwrites in place, so download URLs
never change:

```
https://github.com/mattkravec/moneypuck-data/releases/download/data-v2/<asset>
```

**`data-v2-backups`** — pre-refresh snapshots of the six mid-season files,
named `<stem>__YYYY-MM-DD.parquet`, auto-pruned after 7 days. Marked
prerelease so it doesn't show as "Latest". Only useful for answering "what did
the data look like last Tuesday" — normal consumption should ignore it.

## How the nightly refresh works

`.github/workflows/refresh-current-season.yml` runs
`scripts/refresh_current_season.py` at 10:00 UTC daily (~6am ET, after the last
game of the night and after MoneyPuck's own nightly update). It can also be run
manually from the Actions tab.

Each run:

1. Copies the six current-season assets from `data-v2` into `data-v2-backups`
   under dated names, then prunes backups older than 7 days.
2. Downloads fresh CSVs from MoneyPuck and converts them to Parquet.
3. Uploads to `data-v2` with `--clobber`, overwriting in place.

If a file fails to fetch, the others still upload and the job exits non-zero so
the failure is visible.

Only these six files change mid-season:

```
skaters_2025  goalies_2025  lines_2025  teams_2025  shots_2025
all_teams_2008_to2025
```

The multi-season historical files are closed once a season ends and are never
touched by the nightly job.

### Conversion details worth preserving

- `row_group_size=100_000` on files over 100k rows, so column pushdown can skip
  chunks rather than scanning the whole file.
- `lineId` is cast to `str`. MoneyPuck concatenates player IDs into values that
  overflow int64; leaving it numeric corrupts the column.
- Requests use a browser `User-Agent`. MoneyPuck serves an HTML block page to a
  bare `python-requests` client on the large `all_teams.csv` endpoint, which
  surfaces as a confusing `Error tokenizing data` from pandas. The script
  detects an HTML body and reports the first bytes instead.
- Large files stream to disk rather than being held in memory.

## Annual season rollover

Once per year, when the new season starts — the nightly job will otherwise keep
succeeding while refreshing a finished season, and a green check won't warn you.

1. In `scripts/refresh_current_season.py`, bump the `MPDATA_CURRENT_SEASON`
   default and rename the `_2025` entries in `CURRENT_SEASON_FILES` and
   `SOURCES` to the new year.
2. Convert the season that just ended and fold it into the historical
   multi-season files; add a `shots_<year>.parquet` for the new season.
3. Update `CURRENT_SEASON_FILES` in the skill's `mpdata.py` to match.

No new release tag is needed unless the naming convention itself changes.

## Consuming the data

Via the skill:

```python
import mpdata
mpdata.refresh_current_season()      # pull today's refresh (6 files, not 380 MB)
sk = mpdata.load_skaters(seasons=2025)
```

Or directly:

```python
import pandas as pd
BASE = "https://github.com/mattkravec/moneypuck-data/releases/download/data-v2"
sk = pd.read_parquet(f"{BASE}/skaters_2025.parquet")
sh = pd.read_parquet(f"{BASE}/shots_2025.parquet",
                     columns=["shooterName", "xGoal", "goal", "isPlayoffGame"])
```

### Two quirks that will silently give you wrong numbers

**Shot files include playoff games; aggregate files do not.** Summing goals
from `shots_*` will exceed the matching `skaters_*` total. Filter on
`isPlayoffGame` before comparing across the two.

**Three different shot schemas exist.** Most `shots_YYYY` files carry 124
columns; `shots_2020` and `shots_2024` carry 13 extra derived columns including
`shotGoalProbability` and `homeWinProbability`. Check for presence at runtime
rather than assuming a contiguous range.

Also expect: a duplicated `team` column in `teams_*` (pandas renames the copy to
`team.1`), misspelled source columns that are real and should be used as-is
(`penalityMinutes`, `penalitiesFor/Against`), and `iceTime` in team files versus
`icetime` in skater files. Short seasons — 2012 (48-game lockout) and 2020
(COVID) — are short on purpose, not corrupt.

## Troubleshooting the workflow

| Symptom | Cause |
| --- | --- |
| Workflow doesn't appear in Actions | File isn't on the default branch, or isn't at `.github/workflows/` |
| `Error tokenizing data` | Server returned HTML, not CSV — check the `First bytes:` line in the log |
| `5/6 files` | One source failed; the log names it, the other five still uploaded |
| Backup release never created | `permissions: contents: write` missing from the workflow |
| Scheduled run didn't fire | GitHub delays scheduled jobs under load; cron only runs on the default branch |
