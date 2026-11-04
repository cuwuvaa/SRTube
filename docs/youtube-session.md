# Подключение запасного Google-аккаунта к SRTube

Боту нужен файл сессии YouTube — `cookies.txt` в формате Netscape. Логин и пароль Google вводятся только в браузере. Сохранять их в коде или передавать пользователям бота не требуется. Основная версия теперь работает в Docker; её сессию нужно импортировать в контейнер, а dev-сессию — явно с `.env.dev`.

## Получить файл сессии

1. В Chrome установи расширение **Get cookies.txt LOCALLY**, на которое ссылается [официальный FAQ yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp). Для Firefox в том же FAQ есть ссылка на **cookies.txt**. Разреши расширению работу в приватных окнах.
2. Открой отдельное окно инкогнито и войди на YouTube под запасным Google-аккаунтом. Если YouTube предлагает проверку или CAPTCHA, пройди её в браузере.
3. В той же вкладке открой `https://www.youtube.com/robots.txt`. Других вкладок в этом окне быть не должно.
4. Экспортируй cookies домена `youtube.com` расширением в формате **Netscape / cookies.txt**. Сохрани файл локально, например в `~/Downloads/youtube.cookies.txt`.
5. Закрой окно инкогнито. Не продолжай использовать эту сессию в браузере: YouTube может обновить её cookies, и экспортированный файл перестанет работать. Этот порядок описан в [инструкции yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

Для yt-dlp используй именно запасной аккаунт: YouTube может ограничить используемую сессию или аккаунт. Файл cookies даёт доступ к сессии; держи его локально и не отправляй клиентам бота.

## Импортировать в production на Mac

```sh
control="$HOME/Library/Application Support/SRTube/production"
docker --context desktop-linux compose --project-directory "$control" --env-file "$control/release.env" -f "$control/compose.prod.yaml" exec -T bot python -m srtube --import-cookies - < ~/Downloads/youtube.cookies.txt
```

Новая сессия сохраняется только в закрытый state volume основной версии. Перезапуск после импорта cookies не нужен.

## Импортировать в dev-бота

```sh
cd /Users/subnetmasked/Projects/SRTube
uv run srtube --env-file .env.dev --import-cookies ~/Downloads/youtube.cookies.txt
```

Команда проверит формат и сроки, оставит только cookies YouTube и сохранит файл с правами `600` в `data-dev/auth/youtube.cookies.txt`. Файл не попадает в Git, сборку Docker или очистку кэша. Новые загрузки перечитывают сессию. Для прежнего окружения `.env` всегда указывай его явно — это больше не окружение по умолчанию на рабочей машине.

Проверка с роликом из жалобы:

```sh
uv run srtube --env-file .env.dev --check-youtube 'https://www.youtube.com/watch?v=GRdLzMBhpss'
```

Команда проверяет получение аудиоформата, без скачивания полного трека. После успешной проверки пришли ссылку боту для проверки скачивания и отправки MP3. Также можно открыть `/admin` → «Подключение YouTube» → «Проверить YouTube».

При запуске в Docker файл импортируется прямо в постоянный том:

```sh
docker compose exec -T bot python -m srtube --import-cookies - < ~/Downloads/youtube.cookies.txt
```

Если используешь дополнение с PO Token provider, добавь к командам Compose `-f compose.yaml -f compose.youtube.yaml`.

## Если YouTube всё ещё требует вход

Cookies не гарантируют доступ с любого IP. Воспроизведённый в жалобе отказ включал HTTP 429 и требование входа. Для повторяющихся отказов проверь исходящий IP / прокси сервера и обнови сессию. В некоторых случаях проверку в браузере и скачивание требуется выполнять через один исходящий IP — см. [FAQ yt-dlp про 429](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#http-error-429-too-many-requests-or-402-payment-required).

Для аудиопотоков, требующих PO Token, подготовлен опциональный сервис:

```sh
docker compose -f compose.yaml -f compose.youtube.yaml up -d --build
```

Сервис не публикует порт на сервере. Бот использует его только во внутренней сети Compose. [Руководство yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) рекомендует автоматический provider вместо ручного копирования токена; [документация provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) отдельно уточняет, что он не снимает все проверки входа и IP.
