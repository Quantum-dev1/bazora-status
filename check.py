#!/usr/bin/env python3
"""
Внешний мониторинг bazora.ru → Telegram.

Запускается из GitHub Actions по cron. Проверяет РАЗДЕЛЬНО:
  * Сайт (лендинг)  — GET / (статика, отдаётся nginx напрямую);
  * Бэкенд/API      — GET /ht/ (django-health-check, проходит в приложение)
                      и GET реального API-эндпоинта /data/...;
чтобы понимать, что именно работает, а что нет. Каждая цель проверяется с
ретраями (не ловим ложные падения на кратких флапах). Дополнительно из /ht/
берём покомпонентную health-строку (БД/кэш/redis/миграции/синк МС) — как ИНФО.

Вердикт: 🔴 если недоступен САЙТ ИЛИ БЭКЕНД/API. Предупреждения подсистем
(например, ошибка синка МойСклада — /ht/ при этом отдаёт 200) статус НЕ роняют.

Зависимостей нет — только стандартная библиотека.

Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  — куда слать (секреты).
  BASE_URL   — база (default https://bazora.ru/).
  SITE_URL   — лендинг (default = BASE_URL).
  HEALTH_URL — health-эндпоинт (default <base>/ht/).
  API_URL    — реальный API-эндпоинт (default <base>/data/catalog/products/?limit=1).
  DRY_RUN    — "1" → не слать в Telegram, только печатать.
  RETRIES(3), RETRY_DELAY(5), TIMEOUT(15).
  STATE_FILE — файл с прошлым статусом (default state.json); нужен, чтобы
               слать в Telegram ТОЛЬКО при изменении состояния.
  FORCE_SEND — "1" → отправить статус, даже если ничего не изменилось
               (для ручного workflow_dispatch: «проверить, что бот жив»).
  REMIND_MINUTES(60) — если всё ещё лежит, повторно напоминать не чаще, чем
               раз в столько минут (0 — не напоминать, только на переходах).

Отправка edge-triggered: молчим, пока статус не меняется. Шлём когда:
  сломалось (UP→DOWN), восстановилось (DOWN→UP), либо повторное напоминание
  при затяжном простое. Так бот не спамит «всё работает» каждые 5 минут.

Код возврата: 0 если всё UP, 1 если что-то DOWN (run в Actions краснеет).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL") or "https://bazora.ru/"
SITE_URL = os.environ.get("SITE_URL") or BASE_URL
HEALTH_URL = os.environ.get("HEALTH_URL") or urllib.parse.urljoin(BASE_URL, "/ht/")
API_URL = os.environ.get("API_URL") or urllib.parse.urljoin(
    BASE_URL, "/data/catalog/products/?limit=1"
)
TIMEOUT = float(os.environ.get("TIMEOUT", "15"))
RETRIES = int(os.environ.get("RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")
STATE_FILE = os.environ.get("STATE_FILE") or "state.json"
FORCE_SEND = os.environ.get("FORCE_SEND", "") in ("1", "true", "yes")
REMIND_MINUTES = float(os.environ.get("REMIND_MINUTES", "60"))

UA = "bazora-status-bot (+github actions monitor)"
MSK = timezone(timedelta(hours=3))

HEALTH_LABELS = {
    "DatabaseBackend": "БД",
    "Cache backend: default": "Кэш",
    "MigrationsHealthCheck": "Миграции",
    "RedisHealthCheck": "Redis",
    "MoyskladSync": "Синхр.МС",
}


def _get(url: str, accept: str | None = None):
    """GET → (status_code, body_text, elapsed). status_code=0 при сетевой ошибке."""
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers, method="GET")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            return resp.status, body, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        try:
            body = e.read(65536).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body, time.monotonic() - t0
    except Exception as e:
        return 0, str(e), time.monotonic() - t0


def probe(url: str, label: str) -> dict:
    """Проверка одной цели с ретраями. UP только при 2xx/3xx."""
    last = {"label": label, "up": False, "code": 0, "detail": "", "elapsed": 0.0}
    for attempt in range(1, RETRIES + 1):
        code, body, elapsed = _get(url)
        up = 200 <= code < 400
        last = {"label": label, "up": up, "code": code,
                "detail": body[:200], "elapsed": elapsed}
        print(f"[{label}] attempt {attempt}/{RETRIES}: code={code} time={elapsed:.2f}s up={up}")
        if up:
            return last
        if attempt < RETRIES:
            time.sleep(RETRY_DELAY)
    return last


def health_line() -> str:
    """/ht/ как ИНФО-строка компонентов. Не влияет на вердикт."""
    code, body, _ = _get(HEALTH_URL, accept="application/json")
    if code != 200:
        return f"недоступно ({code or 'нет ответа'}) ⚠️"
    try:
        data = json.loads(body)
    except Exception:
        return "не-JSON ⚠️"
    parts = []
    for key, val in data.items():
        label = HEALTH_LABELS.get(key, key)
        ok = isinstance(val, str) and val.lower() == "working"
        parts.append(f"{label} {'✅' if ok else '⚠️'}")
    return " · ".join(parts) if parts else "пусто"


def _line(res: dict) -> str:
    if res["up"]:
        return f"{res['label']}: ✅ HTTP {res['code']} · {res['elapsed']:.2f}s"
    code = res["code"] or "нет ответа"
    detail = (res["detail"] or "").strip().replace("\n", " ")[:80]
    return f"{res['label']}: 🔴 {code}" + (f" · {detail}" if detail else "")


HEADS = {
    "down": "🔴 BAZORA: сломалось",
    "recovery": "✅ BAZORA: восстановилось",
    "still_down": "🔴 BAZORA: всё ещё лежит",
    "ok": "✅ BAZORA: всё работает",
}


def build_message(site: dict, backend: dict, api: dict, health: str,
                  event: str = "ok", downtime: str = "") -> str:
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M MSK")
    head = HEADS.get(event, HEADS["ok"])
    if event == "recovery" and downtime:
        head += f" (простой ~{downtime})"
    elif event == "still_down" and downtime:
        head += f" (уже ~{downtime})"
    lines = [
        head,
        _line(site),
        _line(backend),
        _line(api),
        f"Компоненты: {health}",
        f"⏱ {now}",
        BASE_URL,
    ]
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if DRY_RUN:
        print("=== DRY_RUN: сообщение НЕ отправлено ===")
        print(text)
        return
    if not token or not chat_id:
        print("ОШИБКА: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(2)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
            print("Telegram: отправлено, HTTP", resp.status)
    except urllib.error.HTTPError as e:
        print("Telegram HTTPError:", e.code, e.read(500).decode("utf-8", "replace"), file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print("Telegram отправка не удалась:", e, file=sys.stderr)
        sys.exit(2)


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("Не удалось записать state:", e, file=sys.stderr)


def _fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def main() -> int:
    site = probe(SITE_URL, "Сайт (лендинг)")
    backend = probe(HEALTH_URL, "Бэкенд (/ht/)")
    api = probe(API_URL, "API (/data)")
    health = health_line()

    all_up = site["up"] and backend["up"] and api["up"]
    current = "up" if all_up else "down"

    prev = _load_state()
    prev_status = prev.get("status")
    now_epoch = int(time.time())
    down_since = prev.get("down_since") or (now_epoch if current == "down" else None)

    # Тип события + слать ли в Telegram (edge-triggered).
    if prev_status is None:
        # Первый запуск (нет истории): молчим, если всё ОК; алертим, если лежит.
        event = "down" if current == "down" else "ok"
        should_send = current == "down"
    elif prev_status != current:
        event = "recovery" if current == "up" else "down"
        should_send = True
    else:
        # Статус не изменился — по умолчанию молчим.
        event = "ok" if current == "up" else "still_down"
        last_alert = prev.get("last_alert") or 0
        should_send = (
            current == "down"
            and REMIND_MINUTES > 0
            and now_epoch - last_alert >= REMIND_MINUTES * 60
        )

    if FORCE_SEND:
        should_send = True

    downtime = _fmt_duration(now_epoch - down_since) if down_since else ""
    message = build_message(site, backend, api, health, event=event, downtime=downtime)

    if should_send:
        send_telegram(message)
    else:
        print("Статус не изменился — в Telegram НЕ шлём.\n" + message)

    _save_state({
        "status": current,
        "down_since": down_since if current == "down" else None,
        "last_alert": now_epoch if should_send else (prev.get("last_alert") or 0),
    })
    return 0 if all_up else 1


if __name__ == "__main__":
    sys.exit(main())
