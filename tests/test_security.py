import importlib.util
import json
import sys
from pathlib import Path

import pytest

from srtube.errors import UserError
from srtube.models import Track
from srtube.process import run_process
from srtube.security import RateLimit, copy_upload, track_directory, upload_snapshot
from srtube.storage import Storage


@pytest.mark.parametrize("name", ["../outside", "/etc/passwd", "a' OR 1=1--", "123456789abc/../x"])
def test_track_paths_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError):
        track_directory(tmp_path, name)


def test_sql_payload_is_data_and_does_not_modify_allowlist(tmp_path):
    storage = Storage(tmp_path)
    try:
        storage.add_users({123}, 1)
        assert storage.get("' OR 1=1; DROP TABLE allowed_users;--", 123) is None
        assert storage.list_users() == [123]
    finally:
        storage.close()


def test_track_rejects_stored_path_and_duration_injection():
    with pytest.raises(ValueError):
        Track("123456789abc", 1, 1, "track", "artist", "url", 10, "../../auth/cookies.txt", 0)
    with pytest.raises(ValueError):
        Track("123456789abc", 1, 1, "track", "artist", "url", float("nan"), "source.wav", 0)


async def test_child_does_not_inherit_tokens_or_shell_commands(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "private-value-never-print")
    monkeypatch.setenv("GITHUB_TOKEN", "private-value-never-print")
    output = await run_process(
        sys.executable,
        "-c",
        "import os,json,sys; print(json.dumps([os.getenv('BOT_TOKEN'),os.getenv('GITHUB_TOKEN'),sys.argv[1]]))",
        "$(touch /tmp/should-not-run)",
        timeout=5,
    )
    assert json.loads(output) == [None, None, "$(touch /tmp/should-not-run)"]


async def test_subprocess_output_limit_and_kernel_file_limit(tmp_path):
    with pytest.raises(UserError, match="размер"):
        await run_process(
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x'*1000000)",
            timeout=5,
            max_output_bytes=1000,
        )
    target = tmp_path / "large"
    try:
        await run_process(
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1],'wb').write(b'x'*1000000)",
            str(target),
            timeout=5,
            max_file_bytes=4096,
        )
    except Exception:
        pass
    assert target.stat().st_size <= 4096


async def test_upload_rejects_renderer_symlinks_and_has_private_copy(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    secret = private / "secret"
    secret.write_bytes(b"private-account-session")
    link = shared / "original.mp3"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        copy_upload(link, private / "copy", 100)
    link.unlink()
    link.write_bytes(b"audio")
    async with upload_snapshot(private, [(link, 100)]) as files:
        link.unlink()
        link.symlink_to(secret)
        assert files[0].read_bytes() == b"audio"
    assert not files[0].exists()


def test_rate_limiter_bounds_memory_and_flood():
    limiter = RateLimit(burst=2, period=1000, capacity=3)
    assert limiter.allow(1) and limiter.allow(1) and not limiter.allow(1)
    for user in range(10):
        limiter.allow(user)
    assert len(limiter.users) == 3


def load_ops():
    path = Path(__file__).resolve().parents[1] / "ops" / "release.py"
    spec = importlib.util.spec_from_file_location("release_ops", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deploy_rolls_back_and_blocks_bad_revision(tmp_path, monkeypatch):
    ops = load_ops()
    old = {"image": ops.IMAGE + "@sha256:" + "a" * 64, "sha": "a" * 40}
    new = {"image": ops.IMAGE + "@sha256:" + "b" * 64, "sha": "b" * 40}
    ops.write_state(tmp_path, {"releases": [old], "blocked": []})
    calls = []

    def fake_compose(root, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("up", "-d") and len([call for call in calls if call[:2] == ("up", "-d")]) == 1:
            raise RuntimeError("unhealthy candidate")
        return ""

    monkeypatch.setattr(ops, "run", lambda *args, **kwargs: "")
    monkeypatch.setattr(ops, "compose", fake_compose)
    with pytest.raises(RuntimeError, match="previous image"):
        ops.deploy(tmp_path, new)
    assert old["image"] in (tmp_path / "release.env").read_text()
    assert ops.state(tmp_path)["blocked"] == [new["sha"]]
    assert ops.state(tmp_path)["releases"] == [old]


def test_deploy_rejects_mutable_or_foreign_images(tmp_path):
    ops = load_ops()
    for image in ["ubuntu:latest", ops.IMAGE + ":main", "ghcr.io/attacker/repo@sha256:" + "a" * 64]:
        with pytest.raises(ValueError):
            ops.deploy(tmp_path, {"image": image, "sha": "a" * 40})


def test_manual_rollback_keeps_current_state_and_blocks_reinstall(tmp_path, monkeypatch):
    ops = load_ops()
    old = {"image": ops.IMAGE + "@sha256:" + "a" * 64, "sha": "a" * 40}
    current = {"image": ops.IMAGE + "@sha256:" + "b" * 64, "sha": "b" * 40}
    ops.write_state(tmp_path, {"releases": [old, current], "blocked": []})
    database = tmp_path / "srtube.sqlite3"
    database.write_bytes(b"current access revocations must survive rollback")
    monkeypatch.setattr(ops, "run", lambda *args, **kwargs: "")
    monkeypatch.setattr(ops, "compose", lambda *args, **kwargs: "")
    ops.deploy(tmp_path, old, rollback=True)
    assert ops.state(tmp_path)["releases"][-1]["image"] == old["image"]
    assert ops.state(tmp_path)["blocked"] == [current["sha"]]
    assert database.read_bytes() == b"current access revocations must survive rollback"


def test_first_failed_release_is_blocked(tmp_path, monkeypatch):
    ops = load_ops()
    release = {"image": ops.IMAGE + "@sha256:" + "a" * 64, "sha": "a" * 40}
    monkeypatch.setattr(ops, "run", lambda *args, **kwargs: "")

    def unhealthy(*args, **kwargs):
        raise RuntimeError("unhealthy")

    monkeypatch.setattr(ops, "compose", unhealthy)
    with pytest.raises(RuntimeError, match="health checks"):
        ops.deploy(tmp_path, release)
    assert ops.state(tmp_path) == {"releases": [], "blocked": [release["sha"]]}
