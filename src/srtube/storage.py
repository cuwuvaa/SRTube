import json
import shutil
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import Track
from .security import media_file, track_directory


class Storage:
    def __init__(self, root: Path, tracks_dir: Path | None = None):
        self.root = root
        self.tracks_dir = tracks_dir or root / "tracks"
        if self.tracks_dir.is_symlink():
            raise ValueError("Tracks directory must not be a symlink")
        self.tracks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "srtube.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tracks "
            "(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, accessed REAL NOT NULL, payload TEXT NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS tracks_user ON tracks(user_id, accessed DESC)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS allowed_users "
            "(user_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL, added_at REAL NOT NULL)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache_files (track_id TEXT PRIMARY KEY, stored_at REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_files "
            "(track_id TEXT NOT NULL, kind TEXT NOT NULL, file_id TEXT NOT NULL, size INTEGER, "
            "PRIMARY KEY(track_id, kind))"
        )
        if not self.get_runtime("cache_files_migrated"):
            self.db.execute(
                "INSERT OR IGNORE INTO cache_files SELECT id, COALESCE(json_extract(payload, '$.created_at'), 0) "
                "FROM tracks WHERE json_valid(payload)"
            )
            self.db.execute("INSERT INTO runtime VALUES ('cache_files_migrated', 'true')")
        self.db.commit()

    def seed_access(self, user_ids: set[int]) -> None:
        if self.get_runtime("access_seeded"):
            return
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO allowed_users VALUES (?, 0, ?)",
                [(user_id, time.time()) for user_id in user_ids],
            )
            self.db.execute("INSERT INTO runtime VALUES ('access_seeded', 'true')")

    def is_allowed(self, user_id: int) -> bool:
        return (
            self.db.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
            is not None
        )

    def add_users(self, user_ids: set[int], added_by: int) -> int:
        with self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO allowed_users VALUES (?, ?, ?)",
                [(user_id, added_by, time.time()) for user_id in user_ids],
            )
            return self.db.total_changes - before

    def remove_users(self, user_ids: set[int]) -> int:
        with self.db:
            before = self.db.total_changes
            self.db.executemany(
                "DELETE FROM allowed_users WHERE user_id = ?", [(value,) for value in user_ids]
            )
            return self.db.total_changes - before

    def list_users(self, offset: int = 0, limit: int = 10) -> list[int]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT user_id FROM allowed_users ORDER BY user_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        ]

    def user_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM allowed_users").fetchone()[0]

    def get_runtime(self, key: str) -> object:
        row = self.db.execute("SELECT value FROM runtime WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_runtime(self, key: str, value: object) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime VALUES (?, ?)", (key, json.dumps(value)))

    def directory(self, track_id: str) -> Path:
        return track_directory(self.tracks_dir, track_id)

    def save(self, track: Track) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?, ?, ?, ?)",
            (track.id, track.user_id, time.time(), json.dumps(asdict(track), ensure_ascii=False)),
        )
        self.db.execute("INSERT OR IGNORE INTO cache_files VALUES (?, ?)", (track.id, time.time()))
        self.db.commit()

    def record(self, track_id: str, user_id: int) -> Track | None:
        row = self.db.execute(
            "SELECT payload FROM tracks WHERE id = ? AND user_id = ?", (track_id, user_id)
        ).fetchone()
        if row is None:
            return None
        try:
            track = Track(**json.loads(row[0]))
            if track.id != track_id or track.user_id != user_id:
                return None
            self.directory(track.id)
        except (ValueError, TypeError, KeyError):
            return None
        return track

    def get(self, track_id: str, user_id: int) -> Track | None:
        track = self.record(track_id, user_id)
        if track is None:
            return None
        try:
            directory = self.directory(track.id)
            source = media_file(directory, track.source_name)
            original = media_file(directory, "original.mp3")
        except ValueError:
            return None
        if not source.is_file() or not original.is_file():
            self.evict(track.id)
            return None
        self.touch(track.id)
        return track

    def latest_record(self, user_id: int) -> Track | None:
        rows = self.db.execute(
            "SELECT id FROM tracks WHERE user_id = ? ORDER BY accessed DESC", (user_id,)
        ).fetchall()
        return next((track for row in rows if (track := self.record(row[0], user_id))), None)

    def remember_file(self, track_id: str, user_id: int, kind: str, file_id: str, size: int | None) -> None:
        if kind not in {"audio", "cover"} or not file_id or len(file_id) > 1024:
            raise ValueError("Invalid Telegram file reference")
        if self.record(track_id, user_id) is None:
            raise ValueError("Unknown track owner")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO telegram_files VALUES (?, ?, ?, ?)", (track_id, kind, file_id, size)
            )

    def file(self, track_id: str, user_id: int, kind: str) -> tuple[str, int | None] | None:
        row = self.db.execute(
            "SELECT file_id, size FROM telegram_files JOIN tracks ON tracks.id = telegram_files.track_id "
            "WHERE track_id = ? AND user_id = ? AND kind = ?",
            (track_id, user_id, kind),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def latest(self, user_id: int) -> Track | None:
        rows = self.db.execute(
            "SELECT id FROM tracks WHERE user_id = ? ORDER BY accessed DESC", (user_id,)
        ).fetchall()
        for row in rows:
            if track := self.get(row[0], user_id):
                return track
        return None

    def cached_url(self, url: str, user_id: int) -> Track | None:
        rows = self.db.execute(
            "SELECT id, payload FROM tracks WHERE user_id = ? ORDER BY accessed DESC", (user_id,)
        ).fetchall()
        for track_id, payload in rows:
            if json.loads(payload)["url"] == url:
                if track := self.get(track_id, user_id):
                    return track
        return None

    def touch(self, track_id: str) -> None:
        self.db.execute("UPDATE tracks SET accessed = ? WHERE id = ?", (time.time(), track_id))
        self.db.commit()

    def remove(self, track_id: str) -> None:
        self.evict(track_id)
        self.db.execute("DELETE FROM telegram_files WHERE track_id = ?", (track_id,))
        self.db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        self.db.commit()

    def evict(self, track_id: str) -> None:
        """Remove heavy files while retaining owner, metadata and Telegram references."""
        try:
            shutil.rmtree(self.directory(track_id))
        except FileNotFoundError:
            pass
        self.db.execute("DELETE FROM cache_files WHERE track_id = ?", (track_id,))
        self.db.commit()

    def cleanup(self, hours: int, max_bytes: int, protected: set[str]) -> None:
        cutoff = time.time() - min(hours, 2) * 3600
        rows = self.db.execute(
            "SELECT id, stored_at FROM tracks JOIN cache_files ON id = track_id ORDER BY accessed"
        ).fetchall()
        known = {row[0] for row in rows}
        sizes = {
            directory.name: sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
            for directory in self.tracks_dir.iterdir()
            if directory.is_dir() and not directory.is_symlink()
        }
        total = sum(sizes.values())
        for track_id, stored_at in rows:
            if track_id not in protected and (stored_at <= cutoff or total > max_bytes):
                self.evict(track_id)
                total -= sizes.get(track_id, 0)
        # Recover directories left behind by an interrupted download or process crash.
        for directory in self.tracks_dir.iterdir():
            if directory.is_symlink():
                continue
            if directory.is_dir() and directory.name not in known | protected:
                if directory.stat().st_mtime <= cutoff:
                    shutil.rmtree(directory, ignore_errors=True)

    def close(self) -> None:
        self.db.close()
