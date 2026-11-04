import argparse
import asyncio
import fcntl
import logging
import sys
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat
from aiogram.utils.token import validate_token
from dotenv import dotenv_values

from .bot import build_dispatcher, cleanup_loop
from .config import Settings
from .errors import UserError
from .health import Health
from .health import check as health_check
from .jobs import Jobs
from .media import Media
from .models import youtube_url
from .storage import Storage
from .youtube import ISSUES, MAX_COOKIE_BYTES, cookie_health, import_cookies


async def serve(
    check_only: bool = False, check_youtube: str | None = None, settings: Settings | None = None
) -> None:
    settings = settings or Settings()
    settings.prepare()
    lock = (settings.data_dir / ".bot.lock").open("a+")
    if not check_only and not check_youtube:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("SRTube уже запущен с этой папкой DATA_DIR.") from None
    storage = Storage(settings.data_dir, settings.tracks_dir)
    media = Media(settings, storage)
    try:
        versions = await media.check_tools()
        if check_youtube:
            try:
                result = await media.diagnose_youtube(youtube_url(check_youtube))
                print(f"OK YouTube: {result['title']}")
            except UserError:
                issue = storage.get_runtime("youtube_last_failure")
                if isinstance(issue, dict):
                    print(ISSUES.get(issue["code"], ISSUES["unknown"]))
                raise
            return
        if check_only:
            for binary, description in versions.items():
                print(f"OK {binary}: {description}")
            print(f"OK yt-dlp: {version('yt-dlp')} (EJS {version('yt-dlp-ejs')})")
            print(f"OK aiogram: {version('aiogram')}")
            print(f"YouTube: {cookie_health(settings.cookies_path).summary}")
            print(f"ADMIN_USER_IDS: {len(settings.admin_ids)} администратор(ов)")
            token = settings.bot_token.get_secret_value()
            if token:
                validate_token(token)
            print("BOT_TOKEN: " + ("задан (формат корректен)" if token else "не задан; добавь токен в .env"))
            return
        token = settings.bot_token.get_secret_value()
        if not token:
            raise ValueError("Добавь BOT_TOKEN из @BotFather в .env. Инструкция — в README.md.")
        validate_token(token)
        if not settings.admin_ids:
            raise ValueError("Задай ADMIN_USER_IDS в .env: числовой Telegram ID владельца админки.")
        jobs = Jobs(settings.max_workers, settings.max_queue)
        dp = build_dispatcher(settings, storage, media, jobs)
        health = Health(settings.data_dir)
        async with Bot(token, default=DefaultBotProperties(parse_mode="HTML")) as bot:
            bot.session.middleware(health)
            await bot.delete_webhook(drop_pending_updates=False)
            commands = [
                BotCommand(command="start", description="Начать работу"),
                BotCommand(command="help", description="Как пользоваться SRTube"),
                BotCommand(command="last", description="Последний готовый трек"),
                BotCommand(command="cancel", description="Отменить ввод процентов"),
                BotCommand(command="id", description="Мой Telegram ID"),
            ]
            await bot.set_my_commands(commands)
            health.touch()
            for admin_id in settings.admin_ids:
                with suppress(TelegramBadRequest):
                    await bot.set_my_commands(
                        [
                            *commands,
                            BotCommand(command="admin", description="Админка и доступ пользователей"),
                        ],
                        scope=BotCommandScopeChat(chat_id=admin_id),
                    )
            cleanup = asyncio.create_task(cleanup_loop(settings, storage, jobs))
            logging.info("SRTube started · workers=%s", settings.max_workers)
            try:
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                    close_bot_session=False,
                    tasks_concurrency_limit=settings.max_workers + settings.max_queue + 32,
                )
            finally:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
                health.path.unlink(missing_ok=True)
    finally:
        storage.close()
        lock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SRTube Telegram bot")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env.dev") if Path(".env.dev").is_file() else Path(".env"),
        help="Отдельный файл окружения; на рабочей машине по умолчанию .env.dev",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check", action="store_true", help="Проверить зависимости без подключения к Telegram"
    )
    modes.add_argument(
        "--check-youtube", metavar="URL", help="Проверить получение аудиоформата YouTube без Telegram"
    )
    modes.add_argument(
        "--import-cookies", metavar="FILE", help="Импортировать Netscape cookies.txt; '-' читает stdin"
    )
    modes.add_argument(
        "--import-bot-token", metavar="FILE", help="Сохранить токен в BOT_TOKEN_FILE; '-' читает stdin"
    )
    modes.add_argument(
        "--healthcheck", action="store_true", help="Проверить работающий polling без сетевого запроса"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if not args.env_file.is_file() and args.env_file != Path(".env"):
            raise ValueError(f"Файл окружения не найден: {args.env_file}")
        settings = Settings(_env_file=args.env_file)
        settings.prepare()
        if settings.environment == "development":
            for candidate in (Path(".env"), Path(".env.prod")):
                if candidate.is_file() and candidate.resolve() != args.env_file.resolve():
                    production_token = dotenv_values(candidate).get("BOT_TOKEN", "") or ""
                    if (
                        production_token
                        and production_token.split(":")[0]
                        == settings.bot_token.get_secret_value().split(":")[0]
                    ):
                        raise ValueError("Dev и production должны использовать разных ботов BotFather.")
        if args.healthcheck:
            raise SystemExit(0 if health_check(settings.data_dir) else 1)
        if args.import_bot_token:
            raw = (
                sys.stdin.buffer.read(257)
                if args.import_bot_token == "-"  # nosec B105
                else Path(args.import_bot_token).read_bytes()
            )
            token = raw.decode().strip()
            validate_token(token)
            if len(raw) > 256:
                raise ValueError("Недопустимый размер токена")
            target = settings.bot_token_file or settings.data_dir / "auth" / "bot.token"
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.touch(mode=0o600)
            target.write_text(token + "\n")
            target.chmod(0o600)
            print("OK: токен сохранён в защищённый файл. Перезапусти бот для применения.")
            return
        if args.import_cookies:
            if args.import_cookies == "-":
                raw = sys.stdin.buffer.read(MAX_COOKIE_BYTES + 1)
            else:
                with Path(args.import_cookies).expanduser().open("rb") as source:
                    raw = source.read(MAX_COOKIE_BYTES + 1)
            health = import_cookies(raw, settings.cookies_path)
            print(f"OK: {health.summary}. Файл сохранён с правами 600: {settings.cookies_path}")
        else:
            asyncio.run(serve(args.check, args.check_youtube, settings))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Startup configuration is useful; upstream errors may contain request secrets.
        if isinstance(exc, (ValueError, UserError)):
            print(f"Ошибка запуска: {exc}")
        else:
            print(f"Ошибка запуска: {type(exc).__name__}. Проверь настройки и подключение.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
