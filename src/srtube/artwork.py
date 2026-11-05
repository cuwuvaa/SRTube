from __future__ import annotations

import asyncio
import json
import math
import warnings
from dataclasses import asdict, dataclass, replace
from io import BytesIO
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .errors import InvalidArtwork, UserError
from .models import VIDEO_ID
from .render_client import request
from .security import media_file

Image.MAX_IMAGE_PIXELS = 8_000_000
MAX_ARTWORK_BYTES = 10_000_000
ARTWORK_ACTIONS = frozenset(
    {"crop", "rotate", "zoom_in", "zoom_out", "left", "right", "up", "down", "brighter", "darker", "reset"}
)


@dataclass(frozen=True)
class CoverSettings:
    crop: bool = False
    rotation: int = 0
    zoom: int = 100
    x: int = 50
    y: int = 50
    brightness: int = 100

    def __post_init__(self) -> None:
        if (
            type(self.crop) is not bool
            or any(
                type(value) is not int
                for value in (self.rotation, self.zoom, self.x, self.y, self.brightness)
            )
            or self.rotation not in {0, 90, 180, 270}
            or not 100 <= self.zoom <= 300
            or not 0 <= self.x <= 100
            or not 0 <= self.y <= 100
            or not 50 <= self.brightness <= 150
        ):
            raise ValueError("Invalid cover settings")

    def change(self, action: str) -> CoverSettings:
        match action:
            case "crop":
                return replace(self, crop=not self.crop, zoom=100, x=50, y=50)
            case "rotate":
                return replace(self, rotation=(self.rotation + 90) % 360)
            case "zoom_in" | "zoom_out":
                return replace(
                    self, zoom=max(100, min(300, self.zoom + (25 if action == "zoom_in" else -25)))
                )
            case "left" | "right":
                return replace(self, x=max(0, min(100, self.x + (10 if action == "right" else -10))))
            case "up" | "down":
                return replace(self, y=max(0, min(100, self.y + (10 if action == "down" else -10))))
            case "brighter" | "darker":
                return replace(
                    self,
                    brightness=max(50, min(150, self.brightness + (10 if action == "brighter" else -10))),
                )
            case "reset":
                return CoverSettings()
            case _:
                raise ValueError("Invalid cover action")


def open_artwork(raw: bytes, formats: tuple[str, ...] = ("JPEG", "PNG", "WEBP")) -> Image.Image:
    if not 0 < len(raw) <= MAX_ARTWORK_BYTES:
        raise UserError("Обложка должна быть не больше 10 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=list(formats)) as opened:
                if opened.width * opened.height > Image.MAX_IMAGE_PIXELS:
                    raise ValueError
                opened.load()
                image = ImageOps.exif_transpose(opened).convert("RGBA")
                background = Image.new("RGBA", image.size, "#12121c")
                return Image.alpha_composite(background, image).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidArtwork() from None


def compose_artwork(image: Image.Image, settings: CoverSettings) -> Image.Image:
    image = image.rotate(-settings.rotation, expand=True)
    if settings.crop or settings.zoom > 100:
        edge = min(image.size) * 100 / settings.zoom
        left = (image.width - edge) * settings.x / 100
        top = (image.height - edge) * settings.y / 100
        cover = image.resize((800, 800), Image.Resampling.LANCZOS, box=(left, top, left + edge, top + edge))
    else:
        cover = ImageOps.fit(image, (800, 800)).filter(ImageFilter.GaussianBlur(35))
        cover = Image.blend(cover, Image.new("RGB", (800, 800), "#12121c"), 0.4)
        foreground = ImageOps.contain(image, (800, 800), Image.Resampling.LANCZOS)
        cover.paste(foreground, ((800 - foreground.width) // 2, (800 - foreground.height) // 2))
    return ImageEnhance.Brightness(cover).enhance(settings.brightness / 100)


def save_artwork(cover: Image.Image, directory: Path) -> None:
    cover.save(directory / "cover.jpg", format="JPEG", quality=93, optimize=True)
    thumbnail = cover.resize((320, 320), Image.Resampling.LANCZOS)
    # Telegram thumbnails must stay below 200 kB even for a noisy uploaded image.
    for quality in (85, 70, 50):
        thumbnail.save(directory / "thumbnail.jpg", format="JPEG", quality=quality, optimize=True)
        if (directory / "thumbnail.jpg").stat().st_size < 200_000:
            break


def prepare_artwork_edit(directory: Path, output: Path, raw: bytes | None, action: str | None) -> None:
    """Prepare all edited assets without changing the currently published cover."""
    if raw is not None:
        image = open_artwork(raw)
        settings = CoverSettings()
    else:
        source = media_file(directory, "cover.source.jpg")
        if not source.is_file():
            source = media_file(directory, "cover.jpg")
        if source.stat().st_size > MAX_ARTWORK_BYTES:
            raise UserError("Обложка должна быть не больше 10 МБ.")
        image = open_artwork(source.read_bytes(), ("JPEG",))
        settings_path = media_file(directory, "cover.settings.json")
        settings = CoverSettings()
        if settings_path.is_file():
            if settings_path.stat().st_size > 1024:
                raise ValueError("Invalid cover settings")
            settings = CoverSettings(**json.loads(settings_path.read_text()))
    if action is not None:
        settings = settings.change(action)
    if raw is not None or not media_file(directory, "cover.source.jpg").exists():
        image.save(output / "cover.source.jpg", format="JPEG", quality=95, optimize=True)
    save_artwork(compose_artwork(image, settings), output)
    (output / "cover.settings.json").write_text(json.dumps(asdict(settings)))


def make_artwork(raw: bytes | None, directory: Path, formats: tuple[str, ...] = ("JPEG",)) -> None:
    if raw:
        try:
            image = open_artwork(raw, formats)
        except UserError:
            image = fallback_cover()
    else:
        image = fallback_cover()
    image.save(directory / "cover.source.jpg", format="JPEG", quality=95, optimize=True)
    save_artwork(compose_artwork(image, CoverSettings()), directory)


def fallback_cover() -> Image.Image:
    cover = Image.new("RGB", (800, 800), "#141522")
    draw = ImageDraw.Draw(cover)
    for index in range(27):
        height = int(35 + 190 * abs(math.sin(index * 0.67)))
        x = 130 + index * 20
        draw.rounded_rectangle((x, 360 - height, x + 10, 360 + height), radius=5, fill="#9d8bff")
    draw.text((245, 650), "SRTube", fill="white", font_size=80)
    return cover


async def download_artwork(video_id: str, directory: Path, renderer_socket: Path | None = None) -> None:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid YouTube ID")
    raw = None
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for quality in ("maxresdefault", "hqdefault"):
                # Constructed from a validated YouTube ID, never an arbitrary remote URL.
                url = f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        continue
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) > 10_000_000:
                            raise ValueError("Thumbnail too large")
                    raw = bytes(chunks)
                    break
    except (aiohttp.ClientError, TimeoutError, ValueError):
        pass
    if renderer_socket:
        if raw:
            (directory / "artwork.input.jpg").write_bytes(raw)
        await request(renderer_socket, {"operation": "artwork", "track_id": directory.name}, 60)
    else:
        await asyncio.to_thread(make_artwork, raw, directory)
