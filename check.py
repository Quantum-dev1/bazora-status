#!/usr/bin/env python3
"""
Внешний мониторинг статуса bazora.ru → Telegram.

Запускается из GitHub Actions по cron. Проверяет главную страницу сайта
(up/down с ретраями, чтобы не ловить ложные срабатывания на флапах маршрута)
и, дополнительно, /ht/ (django-health-check) для покомпонентной инфо-строки.
Затем шлёт текущий статус в Telegram.

Зависимостей нет — только стандартная библиотека.

Переменные окружения:
  TELEGRAM_BOT_TOKEN  — токен бота (@BotFather).            [обязателен, если не DRY_RUN]
  TELEGRAM_CHAT_ID    — id чата/канала для алертов.         [обязателен, если не DRY_RUN]
  TARGET_URL          — что считать «главной» (default https://bazora.ru/).
  HEALTH_URL          — health-эндпоинт (default <origin>/ht/).
  DRY_RUN             — "1" → не слать в Telegram, только печатать (для локального теста).
  RETRIES             — число попыток главной (default 3).
  RETRY_DELAY         — пауза между попытками, сек (default 5).
  TIMEOUT             — таймаут запроса, сек (default 15).

Код возврата: 0 если сайт UP, 1 если DOWN (чтобы run в Actions краснел при падении).
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

TARGET_URL = os.environ.get("TARGET_URL") or "https://bazora.ru/"
HEALTH_URL = os.environ.get("HEALTH_URL") or urllib.parse.urljoin(TARGET_URL, "/ht/")
TIMEOUT = float(os.environ.get("TIMEOUT", "15"))
RETRIES = int(os.environ.get("RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")

UA = "bazora-status-bot (+github actions monitor)"
MSK = timezone(timedelta(hours=3))

# Человеко-читаемые имена компонентов /ht/ (django-health-check).
HEALTH_LABELS = {
    "DatabaseBackend": "БД",
    "Cache backend: default": "Кэш",
    "MigrationsHealthCheck": "Миграции",
    "RedisHealthCheck": "Redis",
    "MoyskladSync": "Синхр.МС",
}


def _get(url: str, accept: str | None = None):
    """GET → (status_code, body_text). status_code=0 при сетевой ошибке."""
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:  # 4xx/5xx — ответ есть
        try:
            body = e.read(65536).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:  # timeout / DNS / conn refused / reset
        return 0, str(e)


def check_site() -> dict:
    """Главная страница с ретраями. UP только при 2xx/3xx."""
    last = {"code": 0, "detail": "", "elapsed": 0.0}
    for attempt in range(1, RETRIES + 1):
        t0 = time.monotonic()
        code, body = _get(TARGET_URL)
        elapsed = time.monotonic() - t0
        last = {"code": code, "detail": body[:200], "elapsed": elapsed}
        up = 200 <= code < 400
        print(f"[site] attempt {attempt}/{RETRIES}: code={code} time={elapsed:.2f}s up={up}")
        if up:
            return {"up": True, **last}
        if attempt < RETRIES:
            time.sleep(RETRY_DELAY)
    return {"up": False, **last}


def check_health() -> str:
    """/ht/ как ИНФО-строка. Не влияет на up/down сайта."""
    code, body = _get(HEALTH_URL, accept="application/json")
    if code != 200:
        return f"health {code or 'нет ответа'} ⚠️"
    try:
        data = json.loads(body)
    except Exception:
        return "health: не-JSON ⚠️"
    parts = []
    for key, val in data.items():
        label = HEALTH_LABELS.get(key, key)
        ok = isinstance(val, str) and val.lower() == "working"
        parts.append(f"{label} {'✅' if ok else '⚠️'}")
    return " · ".join(parts) if parts else "health: пусто"


def build_message(site: dict, health: str) -> str:
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M MSK")
    if site["up"]:
        head = "✅ BAZORA работает"
        site_line = f"Сайт: HTTP {site['code']} · {site['elapsed']:.2f}s"
    else:
        head = "🔴 BAZORA НЕ отвечает"
        code = site["code"] or "нет ответа"
        detail = (site["detail"] or "").strip().replace("\n", " ")[:120]
        site_line = f"Сайт: {code}" + (f" · {detail}" if detail else "")
    lines = [head, site_line, f"Health: {health}", f"⏱ {now}", TARGET_URL]
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
        # не палим токен: печатаем только тело ответа Telegram
        print("Telegram HTTPError:", e.code, e.read(500).decode("utf-8", "replace"), file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print("Telegram отправка не удалась:", e, file=sys.stderr)
        sys.exit(2)


def main() -> int:
    site = check_site()
    health = check_health()
    message = build_message(site, health)
    send_telegram(message)
    return 0 if site["up"] else 1


if __name__ == "__main__":
    sys.exit(main())
