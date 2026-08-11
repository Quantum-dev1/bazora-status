# bazora-status

Внешний мониторинг статуса сайта **[bazora.ru](https://bazora.ru/)** → уведомления в Telegram.

Работает независимо от самого сервера: [GitHub Actions](.github/workflows/monitor.yml)
по расписанию (каждые ~5–10 мин) запускает [`check.py`](check.py), который:

1. Дёргает главную страницу `https://bazora.ru/` **напрямую**, с ретраями
   (3 попытки) — чтобы не ловить ложные падения на кратких сетевых флапах.
2. Дополнительно читает `/ht/` (django-health-check) и показывает состояние
   компонентов (БД, кэш, Redis, миграции, синхронизация МойСклада) — как **инфо**.
   Предупреждения подсистем (например, ошибка синка МС) **не** считаются падением сайта.
3. Шлёт текущий статус в Telegram.

Пример сообщения:

```
✅ BAZORA работает
Сайт: HTTP 200 · 0.42s
Health: Кэш ✅ · БД ✅ · Миграции ✅ · Redis ✅ · Синхр.МС ⚠️
⏱ 2026-08-10 17:05 MSK
https://bazora.ru/
```

или при падении:

```
🔴 BAZORA НЕ отвечает
Сайт: нет ответа · <urlopen error [Errno 111] Connection refused>
Health: health нет ответа ⚠️
⏱ 2026-08-10 17:05 MSK
https://bazora.ru/
```

## Настройка

Нужны два секрета репозитория: **Settings → Secrets and variables → Actions → New repository secret**

| Секрет | Что это |
|--------|---------|
| `TELEGRAM_BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID`   | id чата/канала, куда слать (см. ниже) |

### Как получить токен и chat_id

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получишь **токен**.
2. Добавь бота в нужный чат/канал (в канал — админом с правом постить).
3. Узнать `chat_id`:
   - для личного чата/группы: напиши боту любое сообщение, затем открой
     `https://api.telegram.org/bot<ТОКЕН>/getUpdates` и возьми `chat.id`;
   - для канала: `chat_id` вида `-100...` (или `@username_канала`).

## Проверка

- Ручной прогон: вкладка **Actions → bazora status monitor → Run workflow**.
- Локально (без отправки в Telegram):
  ```bash
  DRY_RUN=1 python3 check.py           # текущий статус bazora.ru
  DRY_RUN=1 TARGET_URL=https://bazora.ru:9999/ python3 check.py   # эмуляция DOWN
  ```

## Настройки (env / repo variables)

| Переменная | По умолчанию | Назначение |
|-----------|--------------|------------|
| `TARGET_URL` | `https://bazora.ru/` | Что мониторить (up/down) |
| `HEALTH_URL` | `<origin>/ht/` | Health-эндпоинт для инфо-строки |
| `RETRIES` | `3` | Попыток до вердикта DOWN |
| `RETRY_DELAY` | `5` | Пауза между попытками, сек |
| `TIMEOUT` | `15` | Таймаут запроса, сек |
| `DRY_RUN` | — | `1` = не слать в Telegram (локальный тест) |

Интервал проверки меняется в [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)
(строка `cron`). Минимум GitHub Actions — 5 минут.

> Зависимостей нет — только стандартная библиотека Python 3. Секреты хранятся
> в GitHub Secrets (шифруются, в код/логи не попадают).
