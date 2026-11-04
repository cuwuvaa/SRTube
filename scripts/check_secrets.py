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


def forbidden(relative: Path, symlink: bool = False) -> bool:
    return (
        symlink
        or relative.suffix in FORBIDDEN
        or any(
            part in {"data", "data-dev", "data-prod", "secrets", "runtime", ".ssh", "backups"}
            for part in relative.parts
        )
        or (relative.name.startswith(".env") and relative.name not in EXAMPLES)
        or ("cookies" in relative.name.lower() and relative.suffix in {".txt", ".json"})
    )


def credential(raw: bytes) -> bool:
    return bool(TOKEN.search(raw) or PRIVATE.search(raw) or b"# Netscape HTTP Cookie File" in raw[:128])


def git(*args: str) -> bytes:
    argv = [shutil.which("git") or "/usr/bin/git", *args]
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, check=True)  # nosec B603
    return result.stdout


def check_index() -> list[str]:
    failures = []
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        relative = Path(name.decode())
        if stage != "0" or forbidden(relative, mode == "120000") or mode == "160000":
            failures.append(f"Forbidden staged path: {relative}")
            continue
        if int(git("cat-file", "-s", oid)) > 2_000_000:
            failures.append(f"Staged file exceeds the source size limit: {relative}")
            continue
        if credential(git("cat-file", "blob", oid)):
            failures.append(f"Credential pattern found in staged file: {relative}")
    return failures


def check(paths: list[Path]) -> list[str]:
    failures = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if forbidden(relative, path.is_symlink()):
            failures.append(f"Forbidden tracked path: {relative}")
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > 2_000_000:
            failures.append(f"Tracked file exceeds the source size limit: {relative}")
            continue
        raw = path.read_bytes()
        if credential(raw):
            failures.append(f"Credential pattern found: {relative}")
    return failures


def main() -> None:
    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    failures = check_index() + check([ROOT / path.decode() for path in files if path])
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("OK: Git candidates contain no forbidden paths or detected credentials")


if __name__ == "__main__":
    main()
