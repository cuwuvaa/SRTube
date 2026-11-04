"""Scan Git candidate paths without printing secret values or reading private runtime data."""

import re
import shutil
import subprocess  # nosec B404
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {".env.example", ".env.dev.example", ".env.prod.example"}
TOKEN = re.compile(rb"(?<!\d)\d{6,16}:[A-Za-z0-9_-]{30,50}(?![A-Za-z0-9_-])")
PRIVATE = re.compile(rb"^-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", re.MULTILINE)
FORBIDDEN = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite3",
    ".sqlite",
    ".db",
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".flac",
    ".webm",
    ".wav",
    ".ogg",
    ".opus",
    ".log",
    ".pid",
}


def check(paths: list[Path]) -> list[str]:
    failures = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if (
            path.is_symlink()
            or path.suffix in FORBIDDEN
            or any(
                part in {"data", "data-dev", "data-prod", "secrets", "runtime", ".ssh", "backups"}
                for part in relative.parts
            )
            or (path.name.startswith(".env") and path.name not in EXAMPLES)
            or ("cookies" in path.name.lower() and path.suffix in {".txt", ".json"})
        ):
            failures.append(f"Forbidden tracked path: {relative}")
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > 2_000_000:
            failures.append(f"Tracked file exceeds the source size limit: {relative}")
            continue
        raw = path.read_bytes()
        if TOKEN.search(raw) or PRIVATE.search(raw) or raw.startswith(b"# Netscape HTTP Cookie File\n"):
            failures.append(f"Credential pattern found: {relative}")
    return failures


def main() -> None:
    argv = [
        shutil.which("git") or "/usr/bin/git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, check=True)  # nosec B603
    files = result.stdout.split(b"\0")
    failures = check([ROOT / path.decode() for path in files if path])
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("OK: Git candidates contain no forbidden paths or detected credentials")


if __name__ == "__main__":
    main()
