# Dev, main и обновление production

Репозиторий: `https://github.com/cuwuvaa/SRTube`. Пока production работает на MacBook в Docker Desktop; арендованный сервер и `~/.ssh` не используются.

## Dev на Mac

Токен dev-бота хранится в `.env.dev`, состояние — в `data-dev`. Основной токен/сессия/база не копируются в dev. Код проверяет, что dev и production не используют одного Telegram-бота.

```sh
git switch dev
uv sync --frozen
uv run srtube --env-file .env.dev --check
uv run srtube --env-file .env.dev
```

Текстовый ввод, команды и тесты можно проверять на dev-боте. Для сетевого теста с cookies импортируй отдельную dev-сессию через `uv run srtube --env-file .env.dev --import-cookies FILE`. Google-сессию основной версии не следует использовать для произвольных экспериментов.

## Выпустить изменение

```sh
git switch -c feature/my-change dev
# Измени код
uv run pytest -q
uv run ruff check src tests ops scripts
uv run bandit -q -r src ops scripts
uv run python scripts/check_secrets.py
git add <конкретные файлы>
git commit -m "Описание изменения"
git push -u origin feature/my-change
gh pr create --base main
```

Проверь CI в PR. После слияния в `main` CI повторно проверит код и соберёт/просканирует нативные AMD64/ARM64-образы. Если всё успешно, создаст маленький `srtube-release` артефакт с commit SHA и immutable image digest. Никакие токены бота или cookies в GitHub/CI не передаются.

Локальный LaunchAgent раз в 120 секунд проверяет успешный CI текущего `main`. Он скачивает **готовый образ**, останавливает прежний polling, создаёт backup базы и запускает новую версию. При провале healthcheck возвращается предыдущий образ. Неуспешный коммит не повторяется бесконечно.

Настоящая защита ветки на текущем GitHub Free/private недоступна; это ограничение описано в [security.md](security.md). Соблюдай PR-процесс вручную. При неуспешном CI `main` не выкатывается.

## Управление основной версией на Mac

Контроллер установлен отдельно от исходников:

```sh
control="$HOME/Library/Application Support/SRTube/production"
/usr/bin/python3 -I "$control/release.py" status --root "$control"
/usr/bin/python3 -I "$control/release.py" poll --root "$control"
/usr/bin/python3 -I "$control/release.py" rollback --root "$control"
```

Rollback переключает образ на предыдущий сохранённый digest, сохраняя актуальную базу и права доступа. Откатанный коммит блокируется: агент не установит его снова при следующей проверке. Новый успешный коммит может выкатиться; для повторной попытки заблокированного коммита используй `resume`.

После самой первой установки предыдущего релиза ещё нет. Он появится после следующего успешного обновления.

Время простоя при обновлении — остановка/старт контейнера; два polling-процесса с одним токеном не запускаются одновременно. Обновления схемы базы должны оставаться совместимыми с предыдущей версией. При несовместимой миграции нужны отдельный план и проверенный backup.

Логи контроллера — `cd.log` в той же папке. Production-конфигурация — `.env.prod`, токен/сессия/SQLite — в закрытом Docker volume state, кэш — в отдельном tracks. Для проверки контейнеров:

```sh
docker --context desktop-linux compose --project-directory "$control" --env-file "$control/release.env" -f "$control/compose.prod.yaml" ps
docker --context desktop-linux stats --no-stream
```

Mac должен быть включён, пользователь — войти в систему, Docker Desktop — работать. При сне/выключении ноутбука бот недоступен; для непрерывной работы позже перенеси эти же контейнеры на VPS.

## Обновить сам контроллер

Изменение файлов `ops/` в dev не меняет автоматически установленный агент. После отдельного просмотра кода контроллер обновляется вручную:

```sh
uv run python ops/install_local.py
```

Агент не исполняет произвольные команды из GitHub, SSH и входящие webhooks не нужны. Для приватного GHCR нужен вход Docker с разрешением `read:packages`; токен хранится менеджером учётных данных Docker, а не в репозитории.
