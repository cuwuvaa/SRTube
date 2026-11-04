"""Check real production container boundaries and Unix IPC without any credentials."""

import argparse
import json
import os
import re
import subprocess  # nosec B404
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], env: dict, source: str | None = None) -> str:
    options = {"env": env, "input": source, "text": True, "capture_output": True, "timeout": 90}
    result = subprocess.run(argv, **options)  # nosec B603
    if result.returncode:
        raise RuntimeError("Container smoke command failed: " + result.stderr[-1500:])
    return result.stdout.strip()


def main(image: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9/.:@_-]+", image):
        raise ValueError("Invalid image reference")
    project = "srtube-check-" + uuid.uuid4().hex[:8]
    env = dict(os.environ, SRTUBE_IMAGE=image, SRTUBE_PROD_ENV=str(ROOT / ".env.prod.example"))
    docker = ["docker"]
    if env.get("SRTUBE_DOCKER_CONTEXT"):
        docker += ["--context", env["SRTUBE_DOCKER_CONTEXT"]]
    compose = [*docker, "compose", "-p", project, "-f", str(ROOT / "compose.prod.yaml")]
    try:
        run([*compose, "up", "-d", "--wait", "--wait-timeout", "60", "renderer"], env)
        cid = run([*compose, "ps", "-q", "renderer"], env)
        inspection = json.loads(run([*docker, "inspect", cid], env))[0]
        config = inspection["HostConfig"]
        if not (
            config["ReadonlyRootfs"]
            and config["NetworkMode"] == "none"
            and config["Memory"] == 512 * 1024**2
            and config["NanoCpus"] == 550000000
            and "ALL" in config["CapDrop"]
        ):
            raise RuntimeError("Production resource/isolation policy not applied")
        # This is a policy check of an isolated tmpfs, not a writable host /tmp directory.
        temporary_mount = "/tmp"  # nosec B108
        destinations = {mount["Destination"] for mount in inspection["Mounts"]}
        if (
            destinations - {temporary_mount} != {"/app/tracks", "/run/srtube"}
            or temporary_mount not in config["Tmpfs"]
        ):
            raise RuntimeError("Renderer received an unexpected mount")
        run(
            [*compose, "exec", "-T", "renderer", "python", "-"],
            env,
            """
from pathlib import Path
import wave
directory=Path('/app/tracks/123456789abc')
directory.mkdir()
with wave.open(str(directory/'source.wav'),'wb') as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(48000)
    output.writeframes(b'\\0\\0'*48000)
if Path('/app/state/auth').exists():
    raise RuntimeError('Renderer has state credentials')
""",
        )
        run(
            [*compose, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "bot", "-"],
            env,
            """
import asyncio
from pathlib import Path
from srtube.render_client import request
async def verify():
    result=await request(Path('/run/srtube/renderer.sock'),
        {'operation':'probe','track_id':'123456789abc','source_name':'source.wav'},30)
    if float(result['format']['duration']) != 1:
        raise RuntimeError('Cross-container Unix IPC failed')
asyncio.run(verify())
""",
        )
        print("OK: renderer is isolated, bounded and reachable by Unix socket")
    finally:
        run([*compose, "down", "--volumes", "--remove-orphans"], env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="srtube:ci")
    main(parser.parse_args().image)
