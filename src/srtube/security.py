import asyncio
import os
import re
import shutil
import stat
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from .errors import UserError

TRACK_ID = re.compile(r"[a-f0-9]{12,32}\Z")
SOURCE_NAME = re.compile(r"source\.(?:webm|m4a|mp4|opus|ogg|aac|wav|mp3|flac)\Z")


def track_directory(root: Path, track_id: str) -> Path:
    if not TRACK_ID.fullmatch(track_id):
        raise ValueError("Invalid track ID")
    path = root / track_id
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Unsafe track directory")
    return path


def media_file(directory: Path, name: str) -> Path:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("Unsafe media filename")
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise ValueError("Unsafe media path")
    return path


def disk_space(path: Path, reserve: int) -> None:
    if shutil.disk_usage(path).free < reserve:
        raise UserError("На сервере временно недостаточно места. Попробуй позже.")


def copy_upload(source: Path, destination: Path, limit: int) -> None:
    """Pin the directory and inode: a renderer must not race an upload into reading state secrets."""
    directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise UserError("Некорректный файл результата.")
            count = 0
            with destination.open("xb") as output:
                while chunk := stream.read(65536):
                    count += len(chunk)
                    if count > limit:
                        raise UserError("Размер результата превысил допустимый лимит.")
                    output.write(chunk)
    finally:
        os.close(directory)


@asynccontextmanager
async def upload_snapshot(root: Path, sources: list[tuple[Path, int]]):
    with tempfile.TemporaryDirectory(prefix="upload-", dir=root) as temporary:
        paths = []
        try:
            for source, limit in sources:
                destination = Path(temporary) / source.name
                await asyncio.to_thread(copy_upload, source, destination, limit)
                paths.append(destination)
            yield paths
        except OSError:
            raise UserError("Файл результата недоступен. Попробуй ещё раз.") from None


class RateLimit:
    """Bounded per-user token bucket, including unauthenticated updates."""

    def __init__(self, burst: int = 8, period: float = 10, capacity: int = 4096):
        self.burst, self.period, self.capacity = burst, period, capacity
        self.users: OrderedDict[int, tuple[float, float]] = OrderedDict()

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        tokens, previous = self.users.pop(user_id, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - previous) * self.burst / self.period)
        permitted = tokens >= 1
        self.users[user_id] = (tokens - 1 if permitted else tokens, now)
        if len(self.users) > self.capacity:
            self.users.popitem(last=False)
        return permitted
