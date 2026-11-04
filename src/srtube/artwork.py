import asyncio
import math
from io import BytesIO
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .models import VIDEO_ID
from .render_client import request

Image.MAX_IMAGE_PIXELS = 8_000_000


def make_artwork(raw: bytes | None, directory: Path) -> None:
    if raw:
        try:
            with Image.open(BytesIO(raw), formats=["JPEG"]) as opened:
                if opened.width * opened.height > 8_000_000:
                    raise ValueError("Artwork too large")
                opened.load()
                image = ImageOps.exif_transpose(opened).convert("RGB")
            background = ImageOps.fit(image, (800, 800)).filter(ImageFilter.GaussianBlur(35))
            background = Image.blend(background, Image.new("RGB", (800, 800), "#12121c"), 0.4)
            foreground = ImageOps.contain(image, (800, 800), Image.Resampling.LANCZOS)
            background.paste(foreground, ((800 - foreground.width) // 2, (800 - foreground.height) // 2))
            cover = background
        except (OSError, ValueError, Image.DecompressionBombError):
            cover = fallback_cover()
    else:
        cover = fallback_cover()
    cover.save(directory / "cover.jpg", format="JPEG", quality=93, optimize=True)
    cover.resize((320, 320), Image.Resampling.LANCZOS).save(
        directory / "thumbnail.jpg",
        format="JPEG",
        quality=85,
        optimize=True,
    )


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
