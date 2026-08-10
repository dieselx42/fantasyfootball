"""Where this instance keeps its data.

Every path the app writes to resolves through here, so a second instance can
run against a completely separate data set without touching the first:

    FF_DATA_DIR=~/fantasy/keg-south  python3 run.py --port 8777
    FF_DATA_DIR=~/fantasy/work-league python3 run.py --port 8778

That is useful today for keeping leagues apart, and it is the first step
toward hosting this for other people: when storage moves from local files to
a database, these are the only four places that have to change.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository root — the default home for everything below.
ROOT = Path(__file__).resolve().parent.parent


def _resolve(env_var: str, default: Path) -> Path:
    override = os.environ.get(env_var)
    return Path(override).expanduser().resolve() if override else default


#: Root for all instance data. Individual paths below can be overridden on
#: their own if you need something non-standard.
DATA_DIR = _resolve("FF_DATA_DIR", ROOT / "data")

#: League configs (JSON, one file per league).
LEAGUES_DIR = _resolve("FF_LEAGUES_DIR", ROOT / "leagues")

#: Uploaded projection CSVs.
PROJECTIONS_DIR = _resolve("FF_PROJECTIONS_DIR", DATA_DIR / "projections")

#: Season state: draft picks, rosters, saved trades.
DB_PATH = _resolve("FF_DB_PATH", DATA_DIR / "fantasy.db")

#: Platform OAuth tokens. Kept out of the repo and chmod 600.
SECRETS_DIR = _resolve("FF_SECRETS_DIR", ROOT / "secrets")


def describe() -> dict[str, str]:
    """Shown in the UI so it is always obvious which data set is loaded."""
    return {
        "data_dir": str(DATA_DIR),
        "leagues_dir": str(LEAGUES_DIR),
        "projections_dir": str(PROJECTIONS_DIR),
        "db_path": str(DB_PATH),
        "custom": bool(
            os.environ.get("FF_DATA_DIR") or os.environ.get("FF_LEAGUES_DIR")
        ),
    }
