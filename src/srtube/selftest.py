"""Offline production-image smoke test, including the minimal FFmpeg build."""

import argparse
import array
import asyncio
import json
import math
import resource
import sys
import tempfile
import time
import wave
from pathlib import Path

from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from pydantic import SecretStr

from .artwork import make_artwork
from .config import Settings
from .media import Media
from .models import Effects, Track
from .storage import Storage


async def verify(seconds: int) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="srtube-selftest-") as temporary:
        settings = Settings(
            _env_file=None,
            environment="legacy",
            bot_token=SecretStr(""),
            bot_token_file=None,
            renderer_socket=None,
            data_dir=Path(temporary),
            min_free_bytes=50_000_000,
            preview_seconds=5,
        )
        settings.prepare()
        storage = Storage(settings.data_dir)
        try:
            media = Media(settings, storage)
            await media.check_tools()
            track = Track(
                "123456789abc",
                1,
                1,
                "Тест",
                "SRTube",
                "https://youtu.be/jNQXAC9IVRw",
                seconds,
                "source.wav",
                time.time(),
            )
            directory = storage.directory(track.id)
            directory.mkdir()
            frame = array.array(
                "h", (int(12000 * math.sin(2 * math.pi * 440 * index / 48000)) for index in range(48000))
            )
            with wave.open(str(directory / track.source_name), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(48000)
                for _ in range(seconds):
                    output.writeframesraw(frame.tobytes())
            make_artwork(None, directory)
            original = await media.render(track)
            if MP3(original).info.bitrate != 320000 or not ID3(original).getall("APIC"):
                raise RuntimeError("MP3 or metadata smoke test failed")
            altered = await media.render(track, Effects(15, 25))
            if abs(MP3(altered).info.length - (seconds / 0.85 + 2.4)) > 0.2:
                raise RuntimeError("Slowed duration smoke test failed")
            voice = await media.render(track, Effects(15, 25), preview=True)
            if (await media.probe(voice))["streams"][0]["codec_name"] != "opus":
                raise RuntimeError("Voice smoke test failed")
        finally:
            storage.close()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "ok": True,
        "source_seconds": seconds,
        "wall_seconds": round(time.monotonic() - started, 2),
        "child_cpu_seconds": round(usage.ru_utime + usage.ru_stime, 2),
        "peak_child_rss_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, choices=range(3, 121), default=3)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(verify(arguments.seconds))))
