"""
CRUD проектов и площадок.

Маршруты:
    GET  /projects              — список проектов
    GET  /projects/new          — форма создания
    POST /projects/new          — сохранить новый проект
    GET  /projects/{slug}       — детальная страница (профиль + площадки)
    GET  /projects/{slug}/edit  — форма редактирования
    POST /projects/{slug}/edit  — сохранить изменения
    POST /projects/{slug}/archive   — архивировать
    POST /projects/{slug}/unarchive — разархивировать
    POST /projects/{slug}/delete    — удалить (запрещено при наличии content)
    POST /projects/{slug}/platform  — сохранить настройки площадки
"""
import json
import re
import unicodedata

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.security import encrypt, decrypt
from app.templates_env import templates

router = APIRouter(prefix="/projects")

PLATFORMS = ("telegram", "vk", "youtube", "instagram", "dzen")

# ─── Утилиты ──────────────────────────────────────────────────────────────────

# Таблица транслитерации ru→en для генерации slug
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii():
            out.append(ch)
        else:
            # Попытка NFD-декомпозиции (ударения и проч.)
            nfd = unicodedata.normalize("NFD", ch)
            out.append(nfd[0] if nfd[0].isascii() else "")
    return "".join(out)


def _make_slug(name: str) -> str:
    slug = _transliterate(name)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "project"


def _unique_slug(base: str, exclude_id: int | None = None) -> str:
    """Возвращает уникальный slug (добавляет -2, -3... при коллизии)."""
    with get_db() as db:
        candidate = base
        n = 1
        while True:
            if exclude_id is not None:
                row = db.execute(
                    "SELECT id FROM projects WHERE slug=? AND id!=?",
                    (candidate, exclude_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT id FROM projects WHERE slug=?", (candidate,)
                ).fetchone()
            if row is None:
                return candidate
            n += 1
            candidate = f"{base}-{n}"


def _profile_completeness(project: dict) -> int:
    """Возвращает процент заполненности профиля (0–100)."""
    fields = ["description", "audience", "tone", "goals", "cta", "links", "themes", "forbidden"]
    filled = sum(1 for f in fields if project.get(f) and str(project[f]).strip())
    return round(filled / len(fields) * 100)


def _platform_display_name(platform: str) -> str:
    return {
        "telegram": "Telegram",
        "vk": "ВКонтакте",
        "youtube": "YouTube",
        "instagram": "Instagram",
        "dzen": "Яндекс.Дзен",
    }.get(platform, platform)


# ─── Список проектов ──────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def projects_list(request: Request):
    with get_db() as db:
        projects = db.execute(
            "SELECT * FROM projects ORDER BY status='active' DESC, updated_at DESC"
        ).fetchall()

        # Для каждого проекта — список включённых площадок
        projects_with_platforms = []
        for p in projects:
            platforms = db.execute(
                "SELECT platform, enabled FROM project_platforms WHERE project_id=?",
                (p["id"],),
            ).fetchall()
            enabled_platforms = [pl["platform"] for pl in platforms if pl["enabled"]]
            projects_with_platforms.append(
                {"project": dict(p), "enabled_platforms": enabled_platforms}
            )

    active = [x for x in projects_with_platforms if x["project"]["status"] == "active"]
    archived = [x for x in projects_with_platforms if x["project"]["status"] == "archived"]

    return templates.TemplateResponse(
        request,
        "projects/list.html",
        {"active": active, "archived": archived},
    )


# ─── Создание ─────────────────────────────────────────────────────────────────


@router.get("/new", response_class=HTMLResponse)
async def project_new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {"project": None, "errors": []},
    )


@router.post("/new")
async def project_new_save(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    audience: str = Form(""),
    tone: str = Form(""),
    goals: str = Form(""),
    cta: str = Form(""),
    links: str = Form(""),
    themes: str = Form(""),
    forbidden: str = Form(""),
    extra: str = Form(""),
):
    errors = []
    name = name.strip()
    if not name:
        errors.append("Название проекта обязательно.")

    if errors:
        return templates.TemplateResponse(
            request,
            "projects/form.html",
            {
                "project": {
                    "name": name, "description": description, "audience": audience,
                    "tone": tone, "goals": goals, "cta": cta, "links": links,
                    "themes": themes, "forbidden": forbidden, "extra": extra,
                },
                "errors": errors,
            },
            status_code=422,
        )

    slug = _unique_slug(_make_slug(name))

    with get_db() as db:
        db.execute(
            """
            INSERT INTO projects
                (slug, name, description, audience, tone, goals, cta,
                 links, themes, forbidden, extra)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (slug, name, description or None, audience or None, tone or None,
             goals or None, cta or None, links or None, themes or None,
             forbidden or None, extra or None),
        )
        project_id = db.execute(
            "SELECT id FROM projects WHERE slug=?", (slug,)
        ).fetchone()["id"]

        # Инициализировать записи для всех 5 площадок
        for platform in PLATFORMS:
            db.execute(
                """
                INSERT OR IGNORE INTO project_platforms
                    (project_id, platform, enabled, mode)
                VALUES (?, ?, 0, 'auto')
                """,
                (project_id, platform),
            )

    return RedirectResponse(f"/projects/{slug}", status_code=303)


# ─── Хелпер: контекст детальной страницы ─────────────────────────────────────


def _build_detail_context(slug: str) -> dict | None:
    """
    Собирает полный контекст для рендера projects/detail.html.
    Возвращает None, если проект не найден.
    Используется во ВСЕХ путях рендера detail.html (GET, 409-ошибка удаления и др.)
    чтобы шаблон всегда получал одинаковый набор переменных.
    """
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE slug=?", (slug,)
        ).fetchone()
        if project is None:
            return None

        # Убедиться, что все 5 площадок есть
        existing_platforms = db.execute(
            "SELECT platform FROM project_platforms WHERE project_id=?",
            (project["id"],),
        ).fetchall()
        existing = {pl["platform"] for pl in existing_platforms}
        for p in PLATFORMS:
            if p not in existing:
                db.execute(
                    "INSERT OR IGNORE INTO project_platforms (project_id, platform) VALUES (?,?)",
                    (project["id"], p),
                )

        platforms = db.execute(
            "SELECT * FROM project_platforms WHERE project_id=? ORDER BY platform",
            (project["id"],),
        ).fetchall()

        content_count = db.execute(
            "SELECT COUNT(*) FROM content WHERE project_id=?", (project["id"],)
        ).fetchone()[0]

        ideas = db.execute(
            "SELECT * FROM ideas WHERE project_id=? ORDER BY created_at DESC",
            (project["id"],),
        ).fetchall()

        draft_count = db.execute(
            "SELECT COUNT(*) FROM content WHERE project_id=? AND status='text_review'",
            (project["id"],),
        ).fetchone()[0]

    project_dict = dict(project)
    completeness = _profile_completeness(project_dict)

    platform_data = []
    for pl in platforms:
        pl_dict = dict(pl)
        pl_dict["display_name"] = _platform_display_name(pl_dict["platform"])
        pl_dict["has_credentials"] = bool(pl_dict.get("credentials"))
        cfg = pl_dict.get("config")
        if cfg:
            try:
                pl_dict["config_parsed"] = json.loads(cfg)
            except (json.JSONDecodeError, TypeError):
                pl_dict["config_parsed"] = {}
        else:
            pl_dict["config_parsed"] = {}
        platform_data.append(pl_dict)

    return {
        "project": project_dict,
        "platforms": platform_data,
        "completeness": completeness,
        "content_count": content_count,
        "ideas": [dict(i) for i in ideas],
        "draft_count": draft_count,
    }


# ─── Детальная страница ───────────────────────────────────────────────────────


@router.get("/{slug}", response_class=HTMLResponse)
async def project_detail(request: Request, slug: str):
    ctx = _build_detail_context(slug)
    if ctx is None:
        return HTMLResponse("Проект не найден", status_code=404)

    return templates.TemplateResponse(request, "projects/detail.html", ctx)


# ─── Редактирование профиля ───────────────────────────────────────────────────


@router.get("/{slug}/edit", response_class=HTMLResponse)
async def project_edit_form(request: Request, slug: str):
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE slug=?", (slug,)
        ).fetchone()
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {"project": dict(project), "errors": []},
    )


@router.post("/{slug}/edit")
async def project_edit_save(
    request: Request,
    slug: str,
    name: str = Form(""),
    description: str = Form(""),
    audience: str = Form(""),
    tone: str = Form(""),
    goals: str = Form(""),
    cta: str = Form(""),
    links: str = Form(""),
    themes: str = Form(""),
    forbidden: str = Form(""),
    extra: str = Form(""),
):
    errors = []
    name = name.strip()
    if not name:
        errors.append("Название проекта обязательно.")

    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE slug=?", (slug,)
        ).fetchone()
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    if errors:
        return templates.TemplateResponse(
            request,
            "projects/form.html",
            {
                "project": {**dict(project), "name": name, "description": description,
                             "audience": audience, "tone": tone, "goals": goals,
                             "cta": cta, "links": links, "themes": themes,
                             "forbidden": forbidden, "extra": extra},
                "errors": errors,
            },
            status_code=422,
        )

    with get_db() as db:
        db.execute(
            """
            UPDATE projects SET
                name=?, description=?, audience=?, tone=?, goals=?, cta=?,
                links=?, themes=?, forbidden=?, extra=?,
                updated_at=datetime('now')
            WHERE slug=?
            """,
            (name, description or None, audience or None, tone or None,
             goals or None, cta or None, links or None, themes or None,
             forbidden or None, extra or None, slug),
        )

    return RedirectResponse(f"/projects/{slug}", status_code=303)


# ─── Архивация / разархивация ─────────────────────────────────────────────────


@router.post("/{slug}/archive")
async def project_archive(slug: str):
    with get_db() as db:
        db.execute(
            "UPDATE projects SET status='archived', updated_at=datetime('now') WHERE slug=?",
            (slug,),
        )
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@router.post("/{slug}/unarchive")
async def project_unarchive(slug: str):
    with get_db() as db:
        db.execute(
            "UPDATE projects SET status='active', updated_at=datetime('now') WHERE slug=?",
            (slug,),
        )
    return RedirectResponse(f"/projects/{slug}", status_code=303)


# ─── Удаление ─────────────────────────────────────────────────────────────────


@router.post("/{slug}/delete")
async def project_delete(request: Request, slug: str):
    with get_db() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE slug=?", (slug,)
        ).fetchone()
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)

        content_count = db.execute(
            "SELECT COUNT(*) FROM content WHERE project_id=?", (project["id"],)
        ).fetchone()[0]

        if content_count > 0:
            ctx = _build_detail_context(slug)
            if ctx is None:
                return HTMLResponse("Проект не найден", status_code=404)
            ctx["delete_error"] = (
                f"Нельзя удалить проект: у него есть {content_count} ед. контента. "
                "Сначала заархивируйте проект."
            )
            return templates.TemplateResponse(
                request,
                "projects/detail.html",
                ctx,
                status_code=409,
            )

        db.execute("DELETE FROM projects WHERE id=?", (project["id"],))

    return RedirectResponse("/projects", status_code=303)


# ─── Настройки площадки ───────────────────────────────────────────────────────


@router.post("/{slug}/platform")
async def platform_save(
    request: Request,
    slug: str,
    platform: str = Form(...),
    enabled: str = Form("0"),
    mode: str = Form("auto"),
    channel_id: str = Form(""),
    group_id: str = Form(""),
    channel: str = Form(""),
    account_name: str = Form(""),
    credentials_token: str = Form(""),
):
    with get_db() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE slug=?", (slug,)
        ).fetchone()
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    if platform not in PLATFORMS:
        return HTMLResponse("Неизвестная площадка", status_code=400)

    # Собрать config JSON в зависимости от платформы
    config_map = {
        "telegram": {"channel_id": channel_id.strip()},
        "vk": {"group_id": group_id.strip()},
        "youtube": {"channel": channel.strip()},
        "instagram": {"account": account_name.strip()},
        "dzen": {"account": account_name.strip()},
    }
    config_json = json.dumps(config_map.get(platform, {}), ensure_ascii=False)

    enabled_int = 1 if enabled in ("1", "on", "true") else 0

    with get_db() as db:
        existing = db.execute(
            "SELECT id, credentials FROM project_platforms WHERE project_id=? AND platform=?",
            (project["id"], platform),
        ).fetchone()

        # Обновить credentials только если передан новый токен (не пустой)
        token = credentials_token.strip()
        if token:
            new_credentials = encrypt(json.dumps({"token": token}, ensure_ascii=False))
        else:
            # Оставить старое значение
            new_credentials = existing["credentials"] if existing else None

        if existing:
            db.execute(
                """
                UPDATE project_platforms
                SET enabled=?, mode=?, config=?, credentials=?
                WHERE id=?
                """,
                (enabled_int, mode, config_json, new_credentials, existing["id"]),
            )
        else:
            db.execute(
                """
                INSERT INTO project_platforms
                    (project_id, platform, enabled, mode, config, credentials)
                VALUES (?,?,?,?,?,?)
                """,
                (project["id"], platform, enabled_int, mode, config_json, new_credentials),
            )

    return RedirectResponse(f"/projects/{slug}", status_code=303)
