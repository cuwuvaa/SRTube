#!/usr/bin/env python3
"""Local outbound-only CD controller. Install a reviewed copy outside the dev checkout."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess  # nosec B404
import tempfile
import time
from pathlib import Path

REPOSITORY = "cuwuvaa/SRTube"
IMAGE = "ghcr.io/cuwuvaa/srtube"
DIGEST = re.compile(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")


def run(*args: str, timeout: int = 300) -> str:
    if args[0] == "docker":
        args = ("docker", "--context", os.environ.get("SRTUBE_DOCKER_CONTEXT", "desktop-linux"), *args[1:])
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,  # nosec B603
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        # Never echo Docker/gh output: it may contain registry credentials or environment data.
        raise RuntimeError(f"Command failed: {Path(args[0]).name} (exit {result.returncode})")
    return result.stdout.strip()


def state(root: Path) -> dict:
    path = root / "history.json"
    return json.loads(path.read_text()) if path.exists() else {"releases": [], "blocked": []}


def write_state(root: Path, data: dict) -> None:
    temporary = root / "history.tmp"
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(root / "history.json")


def compose(root: Path, *args: str, timeout: int = 300) -> str:
    env = root / "release.env"
    return run(
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "--env-file",
        str(env),
        "-f",
        str(root / "compose.prod.yaml"),
        *args,
        timeout=timeout,
    )


def set_image(root: Path, image: str) -> None:
    if not DIGEST.fullmatch(image):
        raise ValueError("Image must be an immutable digest from the configured repository")
    temporary = root / "release.tmp"
    temporary.write_text(f"SRTUBE_IMAGE={image}\nSRTUBE_PROD_ENV={root / '.env.prod'}\n")
    temporary.replace(root / "release.env")


def deploy(root: Path, release: dict, rollback: bool = False) -> None:
    if not DIGEST.fullmatch(release.get("image", "")) or not SHA.fullmatch(release.get("sha", "")):
        raise ValueError("Invalid release metadata")
    history = state(root)
    previous = history["releases"][-1] if history["releases"] else None
    if previous and previous["image"] == release["image"]:
        return
    run("docker", "pull", release["image"], timeout=600)
    set_image(root, release["image"])
    try:
        if previous:
            # Stop polling before recreating containers; two instances must not share a bot token.
            compose(root, "stop", "bot")
            compose(
                root,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "bot",
                "-m",
                "srtube.maintenance",
                "backup",
            )
        compose(root, "up", "-d", "--wait", "--wait-timeout", "150", timeout=210)
        # Give polling time to refresh its heartbeat after initial startup.
        compose(root, "exec", "-T", "bot", "python", "-m", "srtube", "--healthcheck")
    except Exception:
        if previous:
            compose(root, "stop", "bot")
            set_image(root, previous["image"])
            compose(root, "up", "-d", "--wait", "--wait-timeout", "150", timeout=210)
            history["blocked"] = list(dict.fromkeys([*history["blocked"], release["sha"]]))[-20:]
            write_state(root, history)
        raise RuntimeError(
            "Release failed health checks; previous image was restored when available"
        ) from None
    release = {"image": release["image"], "sha": release["sha"], "at": time.time()}
    if rollback and previous:
        history["blocked"] = list(dict.fromkeys([*history["blocked"], previous["sha"]]))[-20:]
    history["releases"] = [*history["releases"], release][-20:]
    write_state(root, history)
    # Keep two locally cached releases. Shared layers are reused, no builds on the host.
    for index, entry in enumerate(reversed(history["releases"][-2:])):
        name = ("current", "previous")[index]
        run("docker", "image", "tag", entry["image"], f"srtube-retain:{name}")
    run("docker", "image", "prune", "--force", "--filter", "until=168h")


def candidate() -> dict | None:
    head = run("gh", "api", f"repos/{REPOSITORY}/git/ref/heads/main", "--jq", ".object.sha", timeout=30)
    if not SHA.fullmatch(head):
        raise ValueError("Invalid main revision")
    runs = json.loads(
        run(
            "gh",
            "api",
            f"repos/{REPOSITORY}/actions/workflows/ci.yml/runs?branch=main&status=success&per_page=5",
            timeout=30,
        )
    )
    for item in runs["workflow_runs"]:
        if item["head_sha"] == head and item["head_repository"]["full_name"].lower() == REPOSITORY.lower():
            with tempfile.TemporaryDirectory(prefix="srtube-release-") as temporary:
                run(
                    "gh",
                    "run",
                    "download",
                    str(int(item["id"])),
                    "--repo",
                    REPOSITORY,
                    "--name",
                    "srtube-release",
                    "--dir",
                    temporary,
                    timeout=60,
                )
                path = Path(temporary) / "release.json"
                if path.is_symlink() or path.stat().st_size > 4096:
                    raise ValueError("Invalid release artifact")
                release = json.loads(path.read_text())
                if release.get("sha") != head or release.get("repository", "").lower() != REPOSITORY.lower():
                    raise ValueError("Release does not match successful main workflow")
                return release
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["poll", "rollback", "status", "resume"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = root / "controller.json"
    if config.is_file():
        os.environ["SRTUBE_DOCKER_CONTEXT"] = json.loads(config.read_text())["docker_context"]
    with (root / "deploy.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        history = state(root)
        if args.action == "status":
            print(json.dumps(history, indent=2))
        elif args.action == "resume":
            history["blocked"] = []
            write_state(root, history)
        elif args.action == "rollback":
            if len(history["releases"]) < 2:
                raise ValueError("No previous release exists")
            deploy(root, history["releases"][-2], rollback=True)
        else:
            release = candidate()
            if release and release["sha"] not in history["blocked"]:
                deploy(root, release)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"CD: {type(error).__name__}: {error}")
        raise SystemExit(1) from None
