import asyncio
import time

import pytest

from srtube.errors import UserError
from srtube.jobs import Jobs
from srtube.models import Track
from srtube.storage import Storage


def seed(storage, user=1, track_id="123456789abc"):
    directory = storage.directory(track_id)
    directory.mkdir()
    (directory / "source.webm").write_bytes(b"source")
    (directory / "original.mp3").write_bytes(b"mp3")
    track = Track(
        track_id, user, user, "Song", "Artist", "https://youtu.be/BaW_jenozKc", 10, "source.webm", time.time()
    )
    storage.save(track)
    return track


def test_persistence_and_owner_isolation(tmp_path):
    storage = Storage(tmp_path)
    track = seed(storage)
    storage.close()
    storage = Storage(tmp_path)
    assert storage.get(track.id, 2) is None
    assert storage.get(track.id, 1) == track
    assert storage.latest(1) == track
    storage.close()


def test_cleanup_protects_active_track_and_prunes_expired(tmp_path):
    storage = Storage(tmp_path)
    track = seed(storage)
    storage.db.execute("UPDATE tracks SET accessed = 0")
    storage.db.commit()
    storage.cleanup(24, 1, {track.id})
    assert storage.directory(track.id).exists()
    storage.cleanup(24, 1, set())
    assert storage.get(track.id, 1) is None
    assert not storage.directory(track.id).exists()
    storage.close()


def test_allowlist_persists_and_env_seed_does_not_restore_revoked_users(tmp_path):
    storage = Storage(tmp_path)
    storage.seed_access({1, 2})
    storage.remove_users({2})
    storage.add_users({3}, 1)
    storage.close()
    storage = Storage(tmp_path)
    storage.seed_access({1, 2, 4})
    assert storage.list_users() == [1, 3]
    assert not storage.is_allowed(2) and not storage.is_allowed(4)
    storage.close()


async def test_queue_limits_and_release_on_cancellation():
    jobs = Jobs(1, 1)
    waiting = asyncio.Event()

    async def queued():
        waiting.set()
        async with jobs.slot(2, "second"):
            pytest.fail("Should still be queued")

    async with jobs.slot(1, "first"):
        with pytest.raises(UserError, match="уже обрабатывается"):
            async with jobs.slot(1, "duplicate"):
                pass
        task = asyncio.create_task(queued())
        await waiting.wait()
        await asyncio.sleep(0)
        assert jobs.protected == {"first", "second"}
        with pytest.raises(UserError, match="очередь заполнена"):
            async with jobs.slot(3, "third"):
                pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert jobs.active == {}
