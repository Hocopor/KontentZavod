"""
Управление HTTP-прокси через веб-интерфейс.

Маршруты:
    GET  /proxies                  — список прокси
    POST /proxies/add              — добавить прокси (textarea, несколько строк)
    POST /proxies/{id}/toggle      — включить/выключить прокси
    POST /proxies/{id}/delete      — удалить прокси
    POST /proxies/{id}/check       — проверить прокси (HTMX → бейдж)
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.db import get_db
from app.security import encrypt, decrypt
from app.services.proxies import parse_proxy_input, check_proxy, mask_proxy_url
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxies")


def _get_all_proxies(db) -> list[dict]:
    """Возвращает все прокси из БД (в том числе отключённые) с маскировкой URL."""
    rows = db.execute(
        """
        SELECT id, label, url_encrypted, enabled, priority, fail_count,
               last_ok_at, last_error, created_at
        FROM proxies
        ORDER BY priority ASC, fail_count ASC, id ASC
        """
    ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        try:
            plain_url = decrypt(d["url_encrypted"])
            d["url_masked"] = mask_proxy_url(plain_url)
            d["url_plain"] = plain_url  # используется только внутри бэкенда
        except Exception:
            d["url_masked"] = "(не удалось расшифровать)"
            d["url_plain"] = ""
        result.append(d)
    return result


def _extract_host_port(url: str) -> str:
    """Извлекает host:port из proxy-URL для использования как label."""
    import re
    # Убираем схему и user:pass@
    m = re.search(r"://(?:[^@]+@)?([\w\-\.]+(?::\d+)?)", url)
    if m:
        return m.group(1)
    return url[:40]


def _render_table(request: Request, **extra) -> HTMLResponse:
    """Рендер фрагмента таблицы прокси (HTMX-ответ для toggle/delete/add)."""
    with get_db() as db:
        proxies = _get_all_proxies(db)
    return templates.TemplateResponse(
        request, "proxies/_table.html", {"proxies": proxies, **extra}
    )


@router.get("", response_class=HTMLResponse)
async def proxies_list(request: Request):
    """Страница со списком всех прокси."""
    with get_db() as db:
        proxies = _get_all_proxies(db)

    return templates.TemplateResponse(
        request,
        "proxies/index.html",
        {"proxies": proxies},
    )


@router.post("/add")
async def proxies_add(
    request: Request,
    raw_urls: str = Form("", alias="raw_urls"),
    label: str = Form("", alias="label"),
):
    """
    Добавляет один или несколько прокси из textarea.
    Дубликаты по host:port не добавляются повторно.
    """
    urls = parse_proxy_input(raw_urls)
    if not urls:
        return _render_table(request, add_error="Не найдено валидных proxy-URL в введённых данных.")

    added = 0
    skipped = 0

    with get_db() as db:
        # Загружаем уже существующие host:port для дедупликации
        existing_rows = db.execute(
            "SELECT url_encrypted FROM proxies"
        ).fetchall()
        existing_hosts: set[str] = set()
        for row in existing_rows:
            try:
                plain = decrypt(row["url_encrypted"])
                existing_hosts.add(_extract_host_port(plain))
            except Exception:
                pass

        for url in urls:
            host_port = _extract_host_port(url)
            if host_port in existing_hosts:
                skipped += 1
                continue

            row_label = label.strip() or host_port
            encrypted = encrypt(url)
            db.execute(
                """
                INSERT INTO proxies (label, url_encrypted, enabled, priority)
                VALUES (?, ?, 1, 100)
                """,
                (row_label, encrypted),
            )
            existing_hosts.add(host_port)
            added += 1

    msg = f"Добавлено прокси: {added}."
    if skipped:
        msg += f" Пропущено дубликатов: {skipped}."
    return _render_table(request, add_result=msg)


@router.post("/{proxy_id}/toggle")
async def proxy_toggle(request: Request, proxy_id: int):
    """Переключает enabled (0 ↔ 1)."""
    with get_db() as db:
        db.execute(
            "UPDATE proxies SET enabled = 1 - enabled WHERE id = ?",
            (proxy_id,),
        )
    return _render_table(request)


@router.post("/{proxy_id}/delete")
async def proxy_delete(request: Request, proxy_id: int):
    """Удаляет прокси из БД."""
    with get_db() as db:
        db.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
    return _render_table(request)


@router.post("/{proxy_id}/check", response_class=HTMLResponse)
async def proxy_check(proxy_id: int):
    """
    Проверяет прокси и возвращает HTML-бейдж.
    Вызывается через HTMX (hx-post).
    """
    with get_db() as db:
        row = db.execute(
            "SELECT url_encrypted FROM proxies WHERE id = ?",
            (proxy_id,),
        ).fetchone()

    if not row:
        return HTMLResponse(
            '<span class="chip chip-error">не найден</span>',
            status_code=404,
        )

    try:
        plain_url = decrypt(row["url_encrypted"])
    except Exception:
        return HTMLResponse('<span class="chip chip-error">ошибка расшифровки</span>')

    ok, msg = check_proxy(plain_url)

    # Обновляем статистику в БД
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        if ok:
            db.execute(
                "UPDATE proxies SET fail_count = 0, last_ok_at = ?, last_error = NULL WHERE id = ?",
                (now, proxy_id),
            )
        else:
            db.execute(
                """
                UPDATE proxies
                SET fail_count = fail_count + 1, last_error = ?
                WHERE id = ?
                """,
                (msg[:500], proxy_id),
            )

    if ok:
        return HTMLResponse(
            f'<span class="chip chip-generated" title="{msg}">✓ {msg[:60]}</span>'
        )
    else:
        return HTMLResponse(
            f'<span class="chip chip-error" title="{msg}">✗ {msg[:80]}</span>'
        )
