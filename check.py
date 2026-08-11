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


def build_message(site: dict, backend: dict, api: dict, health: str) -> str:
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M MSK")
    all_up = site["up"] and backend["up"] and api["up"]
    head = "✅ BAZORA: всё работает" if all_up else "🔴 BAZORA: есть проблема"
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


def main() -> int:
    site = probe(SITE_URL, "Сайт (лендинг)")
    backend = probe(HEALTH_URL, "Бэкенд (/ht/)")
    api = probe(API_URL, "API (/data)")
    health = health_line()
    message = build_message(site, backend, api, health)
    send_telegram(message)
    return 0 if (site["up"] and backend["up"] and api["up"]) else 1


if __name__ == "__main__":
    sys.exit(main())
