from io import BufferedWriter, BytesIO
from pathlib import Path

from .artwork import MAX_ARTWORK_BYTES
from .errors import UserError


class ArtworkBuffer(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ARTWORK_BYTES:
            raise UserError("Обложка должна быть не больше 10 МБ.")
        return super().write(data)


class MP3Writer(BufferedWriter):
    def __init__(self, path: Path, limit: int):
        super().__init__(path.open("xb", buffering=0))
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise UserError(f"MP3 должен быть не больше {self.limit // 1_000_000} МБ.")
        return super().write(data)
