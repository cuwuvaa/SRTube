import json
import time

import pytest

from srtube.config import Settings
from srtube.errors import ProcessError, UserError
from srtube.media import Media
from srtube.storage import Storage
from srtube.youtube import classify_error, cookie_health, import_cookies


def cookies(expires=0, name="SID", value="test-session", domain=".youtube.com"):
    return f"# Netscape HTTP Cookie File\n{domain}\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}\n".encode()


def test_cookie_import_filters_other_sites_and_replaces_atomically(tmp_path):
    path = tmp_path / "auth" / "youtube.cookies.txt"
    health = import_cookies(cookies() + cookies(domain=".example.com") + cookies(domain=".google.com"), path)
    assert health.state == "ready" and health.authenticated
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"example.com" not in path.read_bytes()
    assert b"google.com" not in path.read_bytes()
    assert cookie_health(path) == health
    import_cookies(cookies(value="new-session"), path)
    assert b"new-session" in path.read_bytes()
    assert not list(path.parent.glob(".cookies-*"))


@pytest.mark.parametrize("raw", [b"not a cookies file", cookies(domain=".example.com"), cookies(expires=1)])
def test_bad_import_preserves_working_session(tmp_path, raw):
    path = tmp_path / "cookies.txt"
    import_cookies(cookies(), path)
    before = path.read_bytes()
    with pytest.raises(UserError):
        import_cookies(raw, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("WARNING: cookies warning\nERROR: Requested format is not available", "format"),
        ("ERROR: Sign in to confirm you’re not a bot. Use --cookies", "auth"),
        (
            "WARNING: HTTP Error 429: Too Many Requests\nERROR: Sign in to confirm you’re not a bot",
            "rate_limit",
        ),
        ("ERROR: HTTP Error 403: Forbidden", "forbidden"),
        ("ERROR: Private video. Sign in", "unavailable"),
    ],
)
def test_youtube_failure_classification(stderr, code):
    assert classify_error(stderr) == code


async def test_rate_limit_cooldown_stops_repeated_requests_and_import_resets_it(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, youtube_min_interval_seconds=0)
    storage = Storage(tmp_path)
    media = Media(settings, storage)
    calls = []

    async def fake_process(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ProcessError("ERROR: HTTP Error 429: Too Many Requests")
        assert "--cookies" in args
        return json.dumps({"title": "Available", "duration": 19}).encode()

    monkeypatch.setattr("srtube.media.run_process", fake_process)
    try:
        with pytest.raises(UserError, match="временно"):
            await media.diagnose_youtube("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        assert storage.get_runtime("youtube_last_failure")["code"] == "rate_limit"
        with pytest.raises(UserError, match="через"):
            await media.diagnose_youtube("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        assert len(calls) == 1
        import_cookies(cookies(), settings.cookies_path)
        result = await media.diagnose_youtube("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        assert result["title"] == "Available" and len(calls) == 2
        assert isinstance(storage.get_runtime("youtube_last_success"), float)
    finally:
        storage.close()


async def test_only_one_youtube_session_and_interval_between_starts(tmp_path):
    import asyncio

    settings = Settings(_env_file=None, data_dir=tmp_path, youtube_min_interval_seconds=0.03)
    storage = Storage(tmp_path)
    media = Media(settings, storage)
    starts = []
    active = 0

    async def request(index):
        nonlocal active
        path = tmp_path / str(index)
        path.mkdir()
        async with media.youtube_session(path):
            active += 1
            assert active == 1
            starts.append(time.monotonic())
            await asyncio.sleep(0.005)
            active -= 1

    try:
        await asyncio.gather(request(1), request(2))
        assert starts[1] - starts[0] >= 0.025
    finally:
        storage.close()


def test_po_provider_args_use_recommended_clients_without_losing_cookies(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, youtube_pot_provider_url="http://pot-provider:4416"
    )
    storage = Storage(tmp_path)
    try:
        args = Media(settings, storage).downloader_args(tmp_path / "cookies.txt")
        assert "youtube:player_client=mweb,tv,web_safari" in args
        assert "youtubepot-bgutilhttp:base_url=http://pot-provider:4416" in args
        assert "--cookies" in args
    finally:
        storage.close()
