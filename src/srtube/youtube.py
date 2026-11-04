import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import UserError

MAX_COOKIE_BYTES = 2_000_000
AUTH_NAMES = {"SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}


@dataclass(frozen=True)
class CookieHealth:
    state: str
    authenticated: bool = False

    @property
    def summary(self) -> str:
        return {
            "missing": "Сессия не подключена",
            "invalid": "Файл cookies повреждён или имеет неверный формат",
            "expired": "Срок cookies истёк — нужна новая сессия",
            "ready": "Cookies аккаунта подключены" if self.authenticated else "Подключены гостевые cookies",
        }[self.state]


def cookie_data(raw: bytes) -> tuple[bytes, CookieHealth]:
    if len(raw) > MAX_COOKIE_BYTES:
        raise UserError("Файл cookies слишком большой (максимум 2 МБ).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UserError("Нужен cookies.txt в формате Netscape, кодировка UTF-8.") from None
    output = ["# Netscape HTTP Cookie File", "# SRTube: YouTube session only"]
    youtube = []
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        row = line.removeprefix("#HttpOnly_").split("\t")
        if len(row) != 7 or row[1] not in {"TRUE", "FALSE"} or row[3] not in {"TRUE", "FALSE"}:
            raise UserError("Нужен cookies.txt в формате Netscape: 7 полей, разделённых табуляцией.")
        try:
            expires = int(row[4])
        except ValueError:
            raise UserError("В cookies.txt некорректный срок действия cookies.") from None
        domain = row[0].lstrip(".").lower()
        if domain != "youtube.com" and not domain.endswith(".youtube.com"):
            continue
        output.append(line)
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            youtube.append((row[5], expires))
    if not youtube:
        raise UserError("В файле нет cookies YouTube. Экспортируй сессию на youtube.com.")
    active = [name for name, expires in youtube if expires == 0 or expires > time.time()]
    health = CookieHealth("ready" if active else "expired", bool(AUTH_NAMES.intersection(active)))
    if not active:
        raise UserError("Срок cookies YouTube уже истёк. Экспортируй новую сессию.")
    return ("\n".join(output) + "\n").encode(), health


def cookie_health(path: Path) -> CookieHealth:
    if not path.is_file():
        return CookieHealth("missing")
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_COOKIE_BYTES + 1)
        _, health = cookie_data(raw)
        return health
    except UserError as exc:
        return CookieHealth("expired" if "истёк" in str(exc) else "invalid")
    except OSError:
        return CookieHealth("invalid")


def import_cookies(raw: bytes, target: Path) -> CookieHealth:
    filtered, health = cookie_data(raw)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".cookies-", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(filtered)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return health


def classify_error(stderr: str) -> str:
    """Classify final errors; a warning mentioning cookies must not mask another failure."""
    errors = [line.lower() for line in stderr.splitlines() if line.startswith("ERROR:")]
    final = errors[-1] if errors else stderr.lower()
    if any(
        value in final
        for value in (
            "private video",
            "video unavailable",
            "video has been removed",
            "not available in your country",
        )
    ):
        return "unavailable"
    if any(value in stderr.lower() for value in ("http error 429", "too many requests", "rate limit")):
        return "rate_limit"
    if any(
        value in final
        for value in (
            "sign in",
            "sign-in",
            "not a bot",
            "login_required",
            "age-restricted",
            "confirm your age",
        )
    ):
        return "auth"
    if "403" in final or "forbidden" in final:
        return "forbidden"
    if "requested format" in final or "no video formats" in final:
        return "format"
    if any(
        value in final
        for value in ("timed out", "connection", "unable to download", "network", "name resolution")
    ):
        return "network"
    return "unknown"


ISSUES = {
    "rate_limit": "YouTube ограничил частоту запросов или IP. Подключи cookies; при повторении проверь прокси / IP сервера.",
    "auth": "YouTube требует вход или проверку сессии. Подключи либо обнови cookies отдельного аккаунта.",
    "forbidden": "YouTube вернул HTTP 403. Проверь cookies, PO Token provider и исходящий IP.",
    "format": "Аудиоформат недоступен. Проверь PO Token provider и актуальность yt-dlp.",
    "network": "Сетевая ошибка. Проверь доступ сервера к YouTube и настройки прокси.",
    "cookies": "Файл cookies отсутствует, повреждён или истёк. Импортируй новую сессию.",
    "unavailable": "Конкретное видео закрыто, удалено или недоступно в регионе сервера.",
    "unknown": "Неизвестная ошибка загрузчика. Проверь актуальность yt-dlp и доступ к YouTube.",
}
