"""
Сервис управления HTTP-прокси.

Назначение: обход блокировок api.telegram.org и других ресурсов с российского сервера.

Функции:
    parse_proxy_input  — разбор сырого ввода пользователя в список proxy-URL
    list_active_proxies — активные прокси из БД (url расшифрован)
    check_proxy        — проверка доступности прокси (GET api.telegram.org)
    request_via_proxy  — фейловер-запрос через пул прокси (главная функция)
"""
import logging
import re
import sqlite3
from datetime import datetime

import httpx

from app.security import encrypt, decrypt

logger = logging.getLogger(__name__)

# Регулярное выражение для извлечения значения --proxy из curl-команды.
_CURL_PROXY_RE = re.compile(
    r"""--proxy\s+['"]?(https?://[^\s'"]+|socks5://[^\s'"]+)['"]?""",
    re.IGNORECASE,
)

# Регулярное выражение для «голого» proxy-URL (без curl):
# Обязательно должен содержать либо user:pass@, либо явный :port (цифровой)
# чтобы не матчить обычные URL вроде https://ipv4.webshare.io/
_BARE_PROXY_RE = re.compile(
    r"""(?:
        (?:https?|socks5)://       # схема
        (?:[^@\s]+@)?              # опциональные user:pass@ (их наличие → это прокси)
        [\w\-\.]+                  # host
        :\d{1,5}                   # :port ОБЯЗАТЕЛЕН — отличает прокси от обычного URL
        /?                         # опциональный trailing slash
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def _normalize_url(url: str) -> str:
    """Нормализует proxy-URL: убирает двойной trailing slash, добавляет / в конце."""
    url = url.rstrip("/")
    return url + "/"


def parse_proxy_input(raw: str) -> list[str]:
    """
    Принимает сырой ввод пользователя (одна или несколько строк).

    Каждая строка может быть:
        (а) командой curl: curl --proxy "http://user:pass@host:port/" https://...
        (б) голым URL: http://user:pass@host:port
        (в) socks5://...

    Возвращает список нормализованных proxy-URL. Невалидные строки игнорируются.
    Дубликаты (по нормализованному URL) удаляются.
    """
    result: list[str] = []
    seen_host_port: set[str] = set()

    def _add_url(url: str) -> None:
        """Добавляет URL в результат, если host:port ещё не встречался."""
        norm = _normalize_url(url.strip().strip("'\""))
        hp = _extract_host_port_from_url(norm)
        if hp not in seen_host_port:
            seen_host_port.add(hp)
            result.append(norm)

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Сначала пробуем извлечь из curl --proxy "..."
        curl_matches = _CURL_PROXY_RE.findall(line)
        if curl_matches:
            for match in curl_matches:
                _add_url(match)
            continue  # строка обработана как curl-команда

        # Иначе ищем «голые» proxy-URL (с обязательным :port)
        bare_matches = _BARE_PROXY_RE.findall(line)
        for match in bare_matches:
            _add_url(match)

    return result


def _extract_host_port_from_url(url: str) -> str:
    """Извлекает host:port из proxy-URL для дедупликации."""
    m = re.search(r"://(?:[^@]+@)?([\w\-\.]+:\d+)", url)
    if m:
        return m.group(1)
    return url


def list_active_proxies(db: sqlite3.Connection) -> list[dict]:
    """
    Возвращает активные прокси (enabled=1) из БД.
    URL расшифрован. Порядок: fail_count ASC, priority ASC, id ASC.
    Если таблица proxies не существует (старая БД) — возвращает пустой список.
    """
    try:
        rows = db.execute(
            """
            SELECT id, label, url_encrypted, enabled, priority, fail_count,
                   last_ok_at, last_error, created_at
            FROM proxies
            WHERE enabled = 1
            ORDER BY fail_count ASC, priority ASC, id ASC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # Таблица proxies ещё не создана (старая БД без миграции)
        return []

    result = []
    for row in rows:
        d = dict(row)
        try:
            d["url"] = decrypt(d["url_encrypted"])
        except Exception:
            # Не удалось расшифровать — пропускаем прокси
            logger.warning("Не удалось расшифровать URL прокси id=%s", d["id"])
            continue
        result.append(d)
    return result


def check_proxy(url: str) -> tuple[bool, str]:
    """
    Проверяет доступность прокси.

    Делает GET https://api.telegram.org/ через прокси, timeout 10 сек.
    Любой HTTP-ответ считается успехом (значит, прокси работает и видит внешний интернет).
    Дополнительно пробует определить внешний IP через https://ipv4.webshare.io/.

    socks5 не поддерживается без пакета httpx[socks] — возвращает (False, подсказка).

    Returns:
        (True, "OK, внешний IP: x.x.x.x") или (False, "текст ошибки")
    """
    # socks5 требует дополнительного пакета
    if url.lower().startswith("socks5://"):
        return (
            False,
            "socks5 требует пакета httpx[socks] — пока поддерживаются только http-прокси",
        )

    try:
        # Проверяем через api.telegram.org
        with httpx.Client(proxy=url, timeout=10) as client:
            client.get("https://api.telegram.org/")

        # Пробуем получить внешний IP (некритично)
        ext_ip = "неизвестен"
        try:
            with httpx.Client(proxy=url, timeout=8) as client2:
                resp2 = client2.get("https://ipv4.webshare.io/")
                if resp2.status_code == 200:
                    ext_ip = resp2.text.strip()
        except Exception:
            pass

        return True, f"OK, внешний IP: {ext_ip}"

    except httpx.ProxyError as exc:
        return False, f"Ошибка прокси: {exc}"
    except httpx.ConnectError as exc:
        return False, f"Ошибка подключения: {exc}"
    except httpx.TimeoutException as exc:
        return False, f"Таймаут: {exc}"
    except Exception as exc:
        return False, f"Ошибка: {exc}"


def _mark_proxy_fail(db: sqlite3.Connection, proxy_id: int, error: str) -> None:
    """Увеличивает fail_count и сохраняет last_error."""
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """
        UPDATE proxies
        SET fail_count = fail_count + 1,
            last_error = ?
        WHERE id = ?
        """,
        (error[:500], proxy_id),
    )
    db.commit()


def _mark_proxy_ok(db: sqlite3.Connection, proxy_id: int) -> None:
    """Сбрасывает fail_count и обновляет last_ok_at."""
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """
        UPDATE proxies
        SET fail_count = 0,
            last_ok_at = ?
        WHERE id = ?
        """,
        (now, proxy_id),
    )
    db.commit()


def request_via_proxy(
    method: str,
    url: str,
    _db: sqlite3.Connection | None = None,
    **kwargs,
) -> httpx.Response:
    """
    Выполняет HTTP-запрос через пул прокси с автоматическим фейловером.

    Алгоритм:
        1. Загружает активные прокси из БД (enabled=1, order by fail_count).
        2. Если прокси нет — делает прямой запрос (dev-режим или незаполненный пул).
        3. Для каждого прокси выполняет запрос через httpx.Client(proxy=url).
        4. При сетевых ошибках (ProxyError, ConnectError, ConnectTimeout, ReadTimeout)
           помечает прокси (fail_count+1, last_error) и переходит к следующему.
        5. При успехе — сбрасывает fail_count прокси, возвращает ответ.
        6. HTTP 4xx/5xx от целевого сервера НЕ считаются ошибкой прокси.
        7. Если все прокси умерли — последняя попытка делается напрямую (прямой fallback).

    Args:
        method:  HTTP-метод ("GET", "POST", "PUT", ...)
        url:     целевой URL
        _db:     соединение с БД (если None — открывается своё)
        **kwargs: любые аргументы httpx.Client.request (data, files, json, timeout, ...)

    Returns:
        httpx.Response
    """
    from app.db import get_db

    # Извлекаем таймаут из kwargs отдельно — передаётся в Client.request
    timeout = kwargs.pop("timeout", 30)

    def _direct_request() -> httpx.Response:
        """Прямой запрос без прокси."""
        with httpx.Client(timeout=timeout) as c:
            return c.request(method, url, **kwargs)

    # Открываем БД (используем переданное соединение или создаём своё).
    # ВАЖНО: __enter__ у контекст-менеджера вызывается РОВНО ОДИН раз —
    # повторный вызов у _GeneratorContextManager падает с AttributeError.
    if _db is not None:
        conn = _db
        db_ctx = None
    else:
        db_ctx = get_db()
        conn = db_ctx.__enter__()
    proxies = list_active_proxies(conn)

    try:
        if not proxies:
            logger.debug("Прокси не настроены — прямой запрос на %s", url)
            return _direct_request()

        last_exc: Exception | None = None

        for proxy in proxies:
            proxy_url = proxy["url"]
            proxy_id = proxy["id"]

            # socks5 — пропускаем (нет поддержки без httpx[socks])
            if proxy_url.lower().startswith("socks5://"):
                logger.debug("Пропускаем socks5-прокси id=%s (не поддерживается)", proxy_id)
                continue

            try:
                with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
                    resp = client.request(method, url, **kwargs)

                # Успех — любой HTTP-ответ (4xx/5xx от сервера тоже успех прокси)
                _mark_proxy_ok(conn, proxy_id)
                return resp

            except (
                httpx.ProxyError,
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
            ) as exc:
                err_text = str(exc)[:500]
                logger.warning(
                    "Прокси id=%s (%s) недоступен: %s", proxy_id, proxy_url, err_text
                )
                _mark_proxy_fail(conn, proxy_id, err_text)
                last_exc = exc
                continue

        # Все прокси умерли — прямой fallback
        logger.warning("Все прокси недоступны — пробуем прямой запрос на %s", url)
        return _direct_request()

    finally:
        if db_ctx is not None:
            try:
                db_ctx.__exit__(None, None, None)
            except Exception:
                pass


def mask_proxy_url(url: str) -> str:
    """
    Маскирует креденшелы в proxy-URL для отображения в UI.

    Пример: http://user:secret@host:8080/ → http://user:***@host:8080/
    """
    # Ищем user:pass@ в URL
    masked = re.sub(
        r"(https?://|socks5://)([^:@]+):([^@]+)@",
        r"\1\2:***@",
        url,
        flags=re.IGNORECASE,
    )
    return masked
