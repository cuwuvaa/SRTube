"""Database-only backups; cookies and bot tokens are deliberately excluded."""

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path


def backup(root: Path) -> Path:
    os.umask(0o077)
    target = root / "backups"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = target / ("srtube-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3")
    with sqlite3.connect(root / "srtube.sqlite3") as source, sqlite3.connect(output) as destination:
        source.backup(destination)
    for old in sorted(target.glob("srtube-*.sqlite3"))[:-5]:
        old.unlink()
    return output


if __name__ == "__main__":
    if sys.argv[1:] != ["backup"]:
        raise SystemExit(2)
    backup(Path(os.environ.get("DATA_DIR", "data")))
    print("OK: database backup saved; credentials excluded")
