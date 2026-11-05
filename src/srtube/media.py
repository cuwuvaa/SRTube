import asyncio
import json
import math
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import wave
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, WOAS, ID3NoHeaderError
from mutagen.mp3 import MP3

from .artwork import ARTWORK_ACTIONS, MAX_ARTWORK_BYTES, download_artwork, make_artwork, prepare_artwork_edit
from .config import Settings
from .errors import InvalidMP3, MP3TooLong, ProcessError, UserError
from .models import Effects, Track, clean_text, editable_text, metadata
from .process import run_process
from .render_client import request
from .security import SOURCE_NAME, copy_upload, disk_space, media_file
from .storage import Storage
from .youtube import classify_error, cookie_health

SAMPLE_RATE = 48000
BITRATE = 320_000
MAX_MP3_BYTES = 20_000_000
REVERB_TAIL = 2.4
INPUT_FORMATS = "matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,ogg,mp3,aac,wav,flac"
Progress = Callable[[str], Awaitable[None]]


def estimated_mp3_bytes(duration: float) -> int:
    return math.ceil(duration * BITRATE / 8) + 300_000


def check_size(duration: float, limit: int) -> None:
    if estimated_mp3_bytes(duration) > limit:
        raise UserError(
            "MP3 в 320 кбит/с получится больше лимита Telegram (50 МБ). "
            "Выбери более короткий трек или уменьши замедление."
        )


def write_tags(
    path: Path, track: Track, cover: Path, effects: Effects | None = None, *, replace_cover: bool = True
) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    title = track.title if effects is None else f"{track.title} ({effects.suffix})"
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=track.artist))
    tags.delall("TALB")
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))
    if track.url:
        tags.add(WOAS(url=track.url))
    if replace_cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover.read_bytes()))
    # Preserve ID3v2.4-only fields in an uploaded original; generated MP3s use ID3v2.3.
    version = tags.version[1] if not track.url and effects is None and tags.version[1] in {3, 4} else 3
    tags.save(path, v2_version=version)


def edit_metadata_files(
    directory: Path, track: Track, limit: int, raw: bytes | None, cover_action: str | None
) -> None:
    """Retag a copy, then publish the complete set without re-encoding audio."""
    with tempfile.TemporaryDirectory(prefix="metadata-edit-", dir=directory) as temporary:
        staged = Path(temporary)
        cover = media_file(directory, "cover.jpg")
        names = []
        if raw is not None or cover_action is not None:
            prepare_artwork_edit(directory, staged, raw, cover_action)
            cover = staged / "cover.jpg"
            names = [path.name for path in staged.iterdir()]
        output = staged / "original.mp3"
        copy_upload(media_file(directory, "original.mp3"), output, limit)
        write_tags(output, track, cover, replace_cover=raw is not None or cover_action is not None)
        if output.stat().st_size > limit:
            raise UserError("MP3 с новой обложкой превышает лимит Telegram. Выбери другую обложку.")
        names.append("original.mp3")
        backups = staged / "backups"
        backups.mkdir()
        committed = []
        try:
            for name in names:
                destination = media_file(directory, name)
                if destination.exists():
                    os.link(destination, backups / name, follow_symlinks=False)
                os.replace(staged / name, destination)
                committed.append(name)
        except BaseException:
            for name in reversed(committed):
                backup = backups / name
                if backup.exists():
                    os.replace(backup, directory / name)
                else:
                    (directory / name).unlink(missing_ok=True)
            raise


def import_mp3_files(
    directory: Path, user_id: int, chat_id: int, filename: str, duration_limit: int, byte_limit: int
) -> Track:
    source = media_file(directory, "source.mp3")
    if not 0 < source.stat().st_size <= byte_limit:
        raise UserError(f"MP3 должен быть не больше {byte_limit // 1_000_000} МБ.")
    try:
        audio = MP3(source)
        duration = float(audio.info.length)
        if not math.isfinite(duration) or duration <= 0 or audio.info.layer != 3:
            raise InvalidMP3()
    except (MutagenError, ValueError, OSError):
        raise InvalidMP3() from None
    if duration > duration_limit:
        raise MP3TooLong(duration_limit)

    def text(value: object) -> str:
        return "".join(char for char in clean_text(value) if ord(char) >= 32 and ord(char) != 127)

    def tag(name: str) -> str:
        frame = audio.tags.get(name) if audio.tags else None
        return text(frame.text[0]) if frame and frame.text else ""

    title = tag("TIT2") or text(Path(filename.replace("\\", "/")).stem) or "Без названия"
    track = Track(
        directory.name,
        user_id,
        chat_id,
        title,
        tag("TPE1") or "Неизвестный исполнитель",
        "",
        duration,
        "source.mp3",
        time.time(),
        album=tag("TALB"),
    )
    covers = audio.tags.getall("APIC") if audio.tags else []
    front = next((cover for cover in covers if cover.type == 3), covers[0] if covers else None)
    with tempfile.TemporaryDirectory(prefix="mp3-import-", dir=directory) as temporary:
        staged = Path(temporary)
        make_artwork(front.data if front else None, staged, ("JPEG", "PNG", "WEBP"))
        copy_upload(source, staged / "original.mp3", byte_limit)
        for path in staged.iterdir():
            path.replace(media_file(directory, path.name))
    return track


def make_impulse(path: Path) -> None:
    """Deterministic stereo hall: early reflections + a dense, damped late decay.

    Unit-energy IR, no direct impulse: afir's output is the wet signal only.
    This is convolution reverb, with 2.4 s tail and ~1.7 s RT60.
    """
    # Deterministic audio impulse, not a security token.
    rng = random.Random(7341)  # nosec B311
    channels: list[list[float]] = []
    count = int(SAMPLE_RATE * REVERB_TAIL)
    for channel in range(2):
        samples = [0.0] * count
        previous = 0.0
        for i in range(int(0.035 * SAMPLE_RATE), count):
            t = i / SAMPLE_RATE
            previous = previous * 0.64 + rng.uniform(-1, 1) * 0.36
            ramp = min(1.0, (t - 0.035) / 0.10)
            samples[i] = previous * math.exp(-6.91 * t / 1.7) * ramp * 0.025
        for delay, gain in ((0.023, 0.7), (0.047, 0.5), (0.079, 0.37), (0.113, 0.25)):
            samples[int((delay + channel * 0.003) * SAMPLE_RATE)] += gain
        norm = math.sqrt(sum(value * value for value in samples))
        channels.append([value / norm * 0.8 for value in samples])
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(
            b"".join(
                struct.pack("<hh", round(left * 32767), round(right * 32767))
                for left, right in zip(*channels, strict=True)
            )
        )


def preview_window(duration: float, effects: Effects, seconds: int) -> tuple[float, float, float]:
    """Return input seek, processed pre-roll, output duration; center on source midpoint."""
    length = min(float(seconds), duration / effects.speed)
    source_start = max(0.0, duration / 2 - length * effects.speed / 2)
    preroll = min(3.0, source_start / effects.speed)
    return source_start - preroll * effects.speed, preroll, length


class Media:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.impulse = settings.data_dir / "hall-v1.wav"
        self.afir_normalization = "irnorm=-1"
        self.runtime_args: list[str] = []
        self.youtube_slot = asyncio.Semaphore(1)
        self.youtube_last_started = 0.0
        self.youtube_cooldown_until = 0.0
        self.cookie_signature: tuple[int, int] | None = None

    async def edit_metadata(
        self, track: Track, *, raw: bytes | None = None, cover_action: str | None = None
    ) -> None:
        if editable_text(track.title) != track.title or editable_text(track.artist) != track.artist:
            raise ValueError("Invalid metadata")
        if track.album and editable_text(track.album) != track.album:
            raise ValueError("Invalid album")
        if cover_action is not None and cover_action not in ARTWORK_ACTIONS:
            raise ValueError("Invalid cover action")
        if raw is not None and not 0 < len(raw) <= MAX_ARTWORK_BYTES:
            raise UserError("Обложка должна быть не больше 10 МБ.")
        directory = self.storage.directory(track.id)
        disk_space(self.settings.data_dir, self.settings.min_free_bytes + self.settings.max_upload_bytes)
        if self.settings.renderer_socket:
            input_path = media_file(directory, "artwork.upload")
            try:
                if raw is not None:
                    with input_path.open("xb") as stream:
                        stream.write(raw)
                result = await request(
                    self.settings.renderer_socket,
                    {
                        "operation": "metadata",
                        "track_id": track.id,
                        "track": asdict(track),
                        "upload": raw is not None,
                        "cover_action": cover_action,
                    },
                    self.settings.render_timeout_seconds + 30,
                )
                if result.get("ok") is not True or not media_file(directory, "original.mp3").is_file():
                    raise UserError("Не удалось сохранить метаданные. Попробуй ещё раз.")
            finally:
                input_path.unlink(missing_ok=True)
        else:
            # Finish filesystem publication before a cancellation releases the user's job slot.
            task = asyncio.create_task(
                asyncio.to_thread(
                    edit_metadata_files, directory, track, self.settings.max_upload_bytes, raw, cover_action
                )
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def import_mp3(
        self,
        track_id: str,
        user_id: int,
        chat_id: int,
        filename: str,
        *,
        duration_limit: int | None = None,
        byte_limit: int | None = None,
    ) -> Track:
        duration_limit = self.settings.max_duration_seconds if duration_limit is None else duration_limit
        byte_limit = (
            min(MAX_MP3_BYTES, self.settings.max_download_bytes, self.settings.max_upload_bytes)
            if byte_limit is None
            else byte_limit
        )
        if type(duration_limit) is not int or not 10 <= duration_limit <= 7200:
            raise ValueError("Invalid duration limit")
        if type(byte_limit) is not int or not 0 < byte_limit <= MAX_MP3_BYTES:
            raise ValueError("Invalid MP3 size limit")
        if not isinstance(filename, str) or len(filename) > 1024:
            raise ValueError("Invalid MP3 filename")
        directory = self.storage.directory(track_id)
        if self.settings.renderer_socket:
            result = await request(
                self.settings.renderer_socket,
                {
                    "operation": "import_mp3",
                    "track_id": track_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "filename": filename,
                    "duration_limit": duration_limit,
                    "byte_limit": byte_limit,
                },
                self.settings.render_timeout_seconds + 30,
            )
            track = Track(**result["track"])
            if (
                track.id != track_id
                or track.user_id != user_id
                or track.chat_id != chat_id
                or track.source_name != "source.mp3"
                or track.url
                or track.duration > duration_limit
                or not media_file(directory, "original.mp3").is_file()
            ):
                raise UserError("Не удалось прочитать загруженный MP3. Попробуй ещё раз.")
            return track
        return await asyncio.to_thread(
            import_mp3_files, directory, user_id, chat_id, filename, duration_limit, byte_limit
        )

    async def ingest_mp3(
        self,
        track_id: str,
        user_id: int,
        chat_id: int,
        filename: str,
        download: Callable[[Path], Awaitable[None]],
        progress: Progress,
    ) -> Track:
        directory = self.storage.directory(track_id)
        limit = min(MAX_MP3_BYTES, self.settings.max_download_bytes, self.settings.max_upload_bytes)
        disk_space(self.settings.data_dir, self.settings.min_free_bytes + 2 * limit)
        directory.mkdir(mode=0o700, parents=True)
        complete = False
        try:
            await progress("Скачиваю твой MP3…")
            await download(directory / "source.mp3")
            await progress("Читаю метаданные и обложку MP3…")

            async def finish() -> Track:
                nonlocal complete
                track = await self.import_mp3(track_id, user_id, chat_id, filename)
                self.storage.save(track)
                complete = True
                return track

            task = asyncio.create_task(finish())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        finally:
            if not complete:
                shutil.rmtree(directory, ignore_errors=True)

    async def check_tools(self) -> dict[str, str]:
        for binary in ("ffmpeg", "ffprobe"):
            if not shutil.which(binary):
                raise ValueError(f"Не найден {binary}. Установи FFmpeg и добавь его в PATH.")
        versions = {}
        for binary in ("ffmpeg", "ffprobe"):
            output = await run_process(binary, "-version", timeout=10)
            versions[binary] = output.decode().splitlines()[0]
        output = await run_process("ffmpeg", "-hide_banner", "-h", "filter=afir", timeout=10)
        if b"afir AVOptions" not in output:
            raise ValueError("В сборке FFmpeg отсутствует фильтр afir, необходимый для реверберации.")
        # FFmpeg 6/7 use gtype; FFmpeg 8+ use irnorm (gtype is ignored there).
        if b"irnorm" not in output:
            self.afir_normalization = "gtype=none"
        encoders = await run_process("ffmpeg", "-hide_banner", "-encoders", timeout=10)
        if not all(encoder in encoders for encoder in (b"libmp3lame", b"libopus")):
            raise ValueError("Нужен FFmpeg с кодеками libmp3lame и libopus.")
        if shutil.which("deno"):
            output = await run_process("deno", "--version", timeout=10)
            deno_version = tuple(
                int(value) for value in output.decode().splitlines()[0].split()[1].split(".")[:3]
            )
            if deno_version < (2, 3, 0):
                raise ValueError("Обнови Deno до версии 2.3+ или убери его из PATH и используй Node.js 22+.")
            versions["javascript"] = output.decode().splitlines()[0]
            self.runtime_args = ["--js-runtimes", "deno"]
        elif shutil.which("node"):
            output = await run_process("node", "--version", timeout=10)
            if int(output.decode().strip().lstrip("v").split(".")[0]) < 22:
                raise ValueError("Для YouTube нужен Node.js 22+ или Deno 2.3+.")
            versions["javascript"] = f"node {output.decode().strip()}"
            self.runtime_args = ["--js-runtimes", "node"]
        else:
            raise ValueError("Для YouTube установи Node.js 22+ или Deno 2.3+.")
        if not self.impulse.exists():
            await asyncio.to_thread(make_impulse, self.impulse)
        return versions

    def downloader_args(self, cookies_file: Path | None = None) -> list[str]:
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--no-color",
            "--socket-timeout",
            "20",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--abort-on-unavailable-fragments",
            "--max-filesize",
            str(self.settings.max_download_bytes),
            "--format",
            "bestaudio",
            *self.runtime_args,
        ]
        if cookies_file:
            args += ["--cookies", str(cookies_file)]
        if proxy := self.settings.youtube_proxy.get_secret_value():
            args += ["--proxy", proxy]
        clients = self.settings.youtube_player_clients
        if self.settings.youtube_pot_provider_url and clients == "default":
            clients = "mweb,tv,web_safari"
        args += ["--extractor-args", f"youtube:player_client={clients}", "--sleep-requests", "1"]
        if self.settings.youtube_pot_provider_url:
            args += [
                "--extractor-args",
                f"youtubepot-bgutilhttp:base_url={self.settings.youtube_pot_provider_url}",
            ]
        return args

    def record_youtube_failure(self, code: str) -> None:
        self.storage.set_runtime("youtube_last_failure", {"code": code, "at": time.time()})
        if code in {"auth", "rate_limit", "forbidden"}:
            self.youtube_cooldown_until = time.monotonic() + self.settings.youtube_cooldown_seconds

    @asynccontextmanager
    async def youtube_session(self, directory: Path):
        async with self.youtube_slot:
            source = self.settings.cookies_path
            stat = source.stat() if source.is_file() else None
            signature = (stat.st_mtime_ns, stat.st_size) if stat else None
            if signature != self.cookie_signature:
                self.cookie_signature = signature
                self.youtube_cooldown_until = 0
            remaining = self.youtube_cooldown_until - time.monotonic()
            if remaining > 0:
                raise UserError(
                    f"YouTube временно ограничил загрузку. Попробуй через {math.ceil(remaining)} сек."
                )
            delay = self.settings.youtube_min_interval_seconds - (
                time.monotonic() - self.youtube_last_started
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self.youtube_last_started = time.monotonic()
            cookie_path = None
            private = None
            try:
                health = cookie_health(source)
                if health.state in {"invalid", "expired"} or (
                    self.settings.youtube_cookies_file and health.state == "missing"
                ):
                    self.record_youtube_failure("cookies")
                    raise UserError("Подключение к YouTube требует обновления. Попробуй позже.")
                if health.state == "ready":
                    private = tempfile.TemporaryDirectory(prefix="session-", dir=self.settings.data_dir)
                    cookie_path = Path(private.name) / "youtube-cookies.txt"
                    cookie_path.touch(mode=0o600)
                    await asyncio.to_thread(shutil.copyfile, source, cookie_path)
                yield self.downloader_args(cookie_path)
                self.storage.set_runtime("youtube_last_success", time.time())
            except ProcessError as exc:
                code = classify_error(exc.stderr)
                self.record_youtube_failure(code)
                if code == "unavailable":
                    raise UserError("Видео закрыто, удалено или недоступно в регионе сервера.") from None
                if code == "format":
                    raise UserError(
                        "Не удалось получить аудио этого ролика. Попробуй позже или пришли другое видео."
                    ) from None
                raise UserError("YouTube временно не отдаёт аудио. Попробуй немного позже.") from None
            finally:
                if cookie_path:
                    cookie_path.unlink(missing_ok=True)
                if private:
                    private.cleanup()

    async def diagnose_youtube(self, url: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="youtube-check-", dir=self.settings.data_dir) as temporary:
            async with self.youtube_session(Path(temporary)) as args:
                raw = await run_process(
                    *args,
                    "--skip-download",
                    "--dump-single-json",
                    "--",
                    url,
                    timeout=self.settings.download_timeout_seconds,
                )
                info = json.loads(raw)
                return {"title": info.get("title", ""), "duration": info.get("duration")}

    async def download(self, url: str, directory: Path) -> tuple[dict, Path]:
        async with self.youtube_session(directory) as common_args:
            return await self._download(url, directory, common_args)

    async def _download(self, url: str, directory: Path, common_args: list[str]) -> tuple[dict, Path]:
        private = tempfile.TemporaryDirectory(prefix="metadata-", dir=self.settings.data_dir)
        info_path = Path(private.name) / "download-info.json"
        try:
            raw = await run_process(
                *common_args,
                "--skip-download",
                "--dump-single-json",
                "--",
                url,
                timeout=self.settings.download_timeout_seconds,
                max_file_bytes=self.settings.max_download_bytes,
            )
            info = json.loads(raw)
            if info.get("availability") in {"private", "subscriber_only", "premium_only"}:
                raise UserError(
                    "Бот не выдаёт приватные видео или материалы с доступом по подписке аккаунта."
                )
            duration = info.get("duration")
            if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                raise UserError("Пришли запись видео: прямые эфиры и премьеры пока не поддерживаются.")
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                raise UserError("Не удалось определить длительность видео. Попробуй другую ссылку.")
            if duration > self.settings.max_duration_seconds:
                raise UserError(
                    f"Видео слишком длинное. Лимит — {self.settings.max_duration_seconds // 60} мин."
                )
            check_size(duration, self.settings.max_upload_bytes)
            info_path.touch(mode=0o600)
            info_path.write_bytes(raw)
            output = await run_process(
                *common_args,
                "--load-info-json",
                str(info_path),
                "--no-simulate",
                "--output",
                str(directory / "source.%(ext)s"),
                "--print",
                "after_move:filepath",
                timeout=self.settings.download_timeout_seconds,
                max_file_bytes=self.settings.max_download_bytes,
            )
            lines = output.decode().strip().splitlines()
            source = Path(lines[-1]) if lines else Path()
            if (
                not lines
                or not source.is_file()
                or source.is_symlink()
                or source.parent.resolve() != directory.resolve()
                or not SOURCE_NAME.fullmatch(source.name)
            ):
                raise UserError("Не удалось скачать аудио: оно недоступно или превышает лимит размера.")
            return info, source
        finally:
            info_path.unlink(missing_ok=True)
            private.cleanup()

    async def probe(self, path: Path) -> dict:
        if self.settings.renderer_socket:
            return await request(
                self.settings.renderer_socket,
                {"operation": "probe", "track_id": path.parent.name, "source_name": path.name},
                60,
            )
        raw = await run_process(
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            INPUT_FORMATS,
            "-threads",
            str(self.settings.ffmpeg_threads),
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
            timeout=15,
        )
        return json.loads(raw)

    async def ingest(
        self,
        track_id: str,
        user_id: int,
        chat_id: int,
        url: str,
        progress: Progress,
    ) -> Track:
        directory = self.storage.directory(track_id)
        disk_space(self.settings.data_dir, self.settings.min_free_bytes + self.settings.max_download_bytes)
        directory.mkdir(mode=0o700, parents=True)
        complete = False
        try:
            await progress("Скачиваю лучший доступный аудиопоток YouTube…")
            info, source = await self.download(url, directory)
            details = await self.probe(source)
            duration = float(details["format"]["duration"])
            if not math.isfinite(duration) or not 0 < duration <= self.settings.max_duration_seconds:
                raise UserError("Длительность скачанного аудио превышает допустимый лимит.")
            check_size(duration, self.settings.max_upload_bytes)
            title, artist = metadata(info)
            track = Track(
                track_id,
                user_id,
                chat_id,
                title,
                artist,
                url,
                duration,
                source.name,
                time.time(),
                album=clean_text(info.get("album")),
            )
            await progress("Готовлю MP3 320 кбит/с, метаданные и обложку…")
            await download_artwork(info["id"], directory, self.settings.renderer_socket)
            await self.render(track)
            self.storage.save(track)
            complete = True
            return track
        finally:
            if not complete:
                shutil.rmtree(directory, ignore_errors=True)

    def filter_graph(self, effects: Effects, trim: float = 0, length: float | None = None) -> str:
        graph = (
            f"[0:a]aresample={SAMPLE_RATE}:filter_size=64,"
            f"aformat=channel_layouts=stereo,asetrate={SAMPLE_RATE}*{effects.speed:.4f},"
            f"aresample={SAMPLE_RATE}:filter_size=64[slow];"
        )
        if effects.reverb:
            wet = effects.reverb / 100
            graph += (
                "[slow]asplit=2[dry][send];"
                f"[send]apad=pad_dur={REVERB_TAIL}[padded];"
                f"[padded][1:a]afir={self.afir_normalization}:dry=1:wet=1:irgain=1:"
                "minp=1024:maxp=8192[wet];"
                f"[dry]volume={1 - wet:.4f}[d];[wet]volume={wet:.4f}[w];"
                "[d][w]amix=inputs=2:normalize=0:duration=longest[mix];"
            )
        else:
            graph += "[slow]anull[mix];"
        graph += "[mix]alimiter=limit=0.95:level=false:latency=true"
        if length is not None:
            graph += (
                f",atrim=start={trim:.6f}:duration={length:.6f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:d=0.12,afade=t=out:st={max(0, length - 0.2):.6f}:d=0.2"
            )
        return graph + "[out]"

    async def render(self, track: Track, effects: Effects | None = None, preview: bool = False) -> Path:
        directory = self.storage.directory(track.id)
        source = media_file(directory, track.source_name)
        output = directory / ("preview.ogg" if preview else "effects.mp3" if effects else "original.mp3")
        media_file(directory, output.name)
        disk_space(self.settings.data_dir, self.settings.min_free_bytes)
        if self.settings.renderer_socket:
            result = await request(
                self.settings.renderer_socket,
                {
                    "operation": "render",
                    "track_id": track.id,
                    "track": asdict(track),
                    "effects": asdict(effects) if effects else None,
                    "preview": preview,
                },
                self.settings.render_timeout_seconds + 30,
            )
            if result.get("file") != output.name or not output.is_file() or output.is_symlink():
                raise UserError("Не удалось получить обработанный файл.")
            return output
        total = track.duration / (effects.speed if effects else 1)
        if effects and effects.reverb:
            total += REVERB_TAIL
        if not preview:
            check_size(total, self.settings.max_upload_bytes)
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-threads",
            str(self.settings.ffmpeg_threads),
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            INPUT_FORMATS,
        ]
        trim, length = 0.0, None
        if preview:
            if effects is None:
                raise ValueError("Preview requires effect settings")
            seek, trim, length = preview_window(track.duration, effects, self.settings.preview_seconds)
            args += ["-ss", f"{seek:.6f}"]
        args += ["-i", str(source)]
        if effects:
            if effects.reverb:
                args += ["-i", str(self.impulse)]
            args += [
                "-filter_complex_threads",
                "1",
                "-filter_complex",
                self.filter_graph(effects, trim, length),
                "-map",
                "[out]",
            ]
        else:
            args += ["-map", "0:a:0"]
        args += ["-vn", "-map_metadata", "-1"]
        if preview:
            args += [
                "-t",
                f"{length:.6f}",
                "-c:a",
                "libopus",
                "-b:a",
                "96k",
                "-vbr",
                "on",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        else:
            args += ["-c:a", "libmp3lame", "-b:a", "320k"]
        args += ["-fs", str(self.settings.max_upload_bytes), str(output)]
        try:
            await run_process(
                *args,
                timeout=self.settings.render_timeout_seconds,
                max_file_bytes=self.settings.max_upload_bytes + 300_000,
            )
            if not preview:
                await asyncio.to_thread(write_tags, output, track, directory / "cover.jpg", effects)
            if output.stat().st_size > self.settings.max_upload_bytes:
                raise UserError("Получившийся файл превышает лимит Telegram (50 МБ). Уменьши замедление.")
            return output
        except ProcessError:
            output.unlink(missing_ok=True)
            raise UserError(
                "Не удалось обработать аудио. Попробуй другие параметры или другое видео."
            ) from None
        except BaseException:
            output.unlink(missing_ok=True)
            raise
