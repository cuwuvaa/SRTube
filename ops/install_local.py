"""Copy the CD controller outside the dev checkout; prepare a disabled LaunchAgent."""

import json
import os
import plistlib
import shutil
import subprocess  # nosec B404
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.cuwuvaa.srtube.production.cd"


def install() -> tuple[Path, Path]:
    os.umask(0o077)
    control = Path.home() / "Library" / "Application Support" / "SRTube" / "production"
    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    argv = [shutil.which("docker") or "/usr/local/bin/docker", "context", "show"]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)  # nosec B603
    context = result.stdout.strip()
    (control / "controller.json").write_text(json.dumps({"docker_context": context}))
    for source, name in (
        (ROOT / "ops/release.py", "release.py"),
        (ROOT / "compose.prod.yaml", "compose.prod.yaml"),
    ):
        temporary = control / (name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(control / name)
    config = control / ".env.prod"
    if not config.exists():
        config.write_text((ROOT / ".env.prod.example").read_text())
    config.chmod(0o600)
    (control / "cd.log").touch(mode=0o600)
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / (LABEL + ".plist")
    target.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [
                    "/usr/bin/python3",
                    "-I",
                    str(control / "release.py"),
                    "poll",
                    "--root",
                    str(control),
                ],
                "WorkingDirectory": str(control),
                "RunAtLoad": True,
                "StartInterval": 120,
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
                "StandardOutPath": str(control / "cd.log"),
                "StandardErrorPath": str(control / "cd.log"),
                "ProcessType": "Background",
            }
        )
    )
    target.chmod(0o600)
    return control, target


if __name__ == "__main__":
    control, agent = install()
    print(f"Controller installed: {control}")
    print(f"LaunchAgent prepared (not started): {agent}")
