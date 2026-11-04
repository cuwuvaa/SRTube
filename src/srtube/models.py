import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .errors import UserError
from .security import SOURCE_NAME, TRACK_ID

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_url(text: str) -> str:
    """Canonicalize one video URL. Never forward arbitrary hosts to yt-dlp."""
    text = text.strip()
    if len(text) > 2048 or any(ord(character) < 32 for character in text):
        raise UserError("Пришли одну короткую ссылку на YouTube.")
    if not text.startswith(("https://", "http://")):
        text = "https://" + text
    try:
        parsed = urlsplit(text)
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError
        host = (parsed.hostname or "").lower()
        parts = parsed.path.strip("/").split("/")
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = parts[0] if len(parts) == 1 else ""
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
                video_id = parts[1]
            else:
                video_id = ""
        else:
            video_id = ""
    except ValueError:
        video_id = ""
    if not VIDEO_ID.fullmatch(video_id):
        raise UserError("Пришли одну ссылку на видео YouTube: youtube.com/watch?v=… или youtu.be/…")
    return f"https://www.youtube.com/watch?v={video_id}"


def clean_text(value: object, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def metadata(info: dict) -> tuple[str, str]:
    title = clean_text(info.get("track"))
    artist = clean_text(info.get("artist") or info.get("creator"))
    video_title = clean_text(info.get("title")) or "Без названия"
    # Explicit music metadata takes precedence over guesses from a video title.
    if not title:
        match = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", video_title)
        if match:
            inferred_artist, title = match.groups()
            artist = artist or inferred_artist
        else:
            title = video_title
        title = (
            re.sub(
                r"\s*[\[(](?:official\s+(?:music\s+)?(?:video|audio)|lyrics?|visuali[sz]er|hd|4k)[\])]",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()
            or video_title
        )
    artist = artist or clean_text(info.get("uploader") or info.get("channel")) or "Неизвестный исполнитель"
    artist = re.sub(r"\s*-\s*Topic$", "", artist).strip() or "Неизвестный исполнитель"
    return clean_text(title), clean_text(artist)


@dataclass(frozen=True)
class Effects:
    slowed: int = 15
    reverb: int = 25

    def __post_init__(self) -> None:
        if not 0 <= self.slowed <= 50 or not 0 <= self.reverb <= 100:
            raise UserError("Slowed: от 0 до 50%. Reverb: от 0 до 100%.")

    @property
    def speed(self) -> float:
        return 1 - self.slowed / 100

    @property
    def suffix(self) -> str:
        return f"slowed {self.slowed}% + reverb {self.reverb}%"


def parse_effects(text: str) -> Effects:
    match = re.fullmatch(r"\s*(\d{1,3})\s*%?\s*[,;/\s]\s*(\d{1,3})\s*%?\s*", text)
    if not match:
        raise UserError("Введи два целых процента: например, 17 35 (slowed, затем reverb).")
    return Effects(int(match[1]), int(match[2]))


@dataclass(frozen=True)
class Track:
    id: str
    user_id: int
    chat_id: int
    title: str
    artist: str
    url: str
    duration: float
    source_name: str
    created_at: float

    def __post_init__(self) -> None:
        import math

        if not TRACK_ID.fullmatch(self.id) or not SOURCE_NAME.fullmatch(self.source_name):
            raise ValueError("Invalid stored media path")
        if not math.isfinite(self.duration) or not 0 < self.duration <= 7200:
            raise ValueError("Invalid track duration")

    @property
    def filename(self) -> str:
        name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", f"{self.artist} — {self.title}")
        return name[:150].strip(" .") or "SRTube"
