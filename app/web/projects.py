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
    POST /projects/{slug}/platforms/{platform}/check — проверить подключение (HTMX)
"""
import json
import re
import unicodedata

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.llm import LLMError
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
    """Шаг 1: минимальная форма (название, описание, цель) с выбором: ИИ или вручную."""
    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {"project": None, "errors": [], "show_full": False},
    )


@router.post("/generate-profile", response_class=HTMLResponse)
async def project_generate_profile(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    goals: str = Form(""),
    manual: str = Form("0"),
):
    """
    HTMX-эндпоинт: генерирует маркетинговый профиль через ИИ (или возвращает пустую форму
    при manual=1) и возвращает HTML-фрагмент с заполненной полной формой создания проекта.
    """
    from app.pipeline.profile import generate_profile

    step1 = {"name": name.strip(), "description": description.strip(), "goals": goals.strip()}

    if manual == "1":
        # Вернуть пустую форму без ИИ
        project_data = {
            "name": step1["name"],
            "description": step1["description"],
            "goals": step1["goals"],
            "audience": "", "tone": "", "cta": "",
            "themes": "", "forbidden": "", "extra": "", "links": "",
        }
        return templates.TemplateResponse(
            request,
            "projects/_profile_fragment.html",
            {
                "project": project_data,
                "ai_generated": False,
                "llm_error": None,
                "step1": step1,
            },
        )

    try:
        profile = generate_profile(
            name=step1["name"],
            description=step1["description"],
            goals=step1["goals"],
        )
    except LLMError as exc:
        return templates.TemplateResponse(
            request,
            "projects/_profile_fragment.html",
            {
                "project": None,
                "ai_generated": False,
                "llm_error": str(exc),
                "step1": step1,
            },
        )

    project_data = {
        "name": step1["name"],
        "description": step1["description"],
        "goals": step1["goals"],
        "audience": profile.get("audience", ""),
        "tone": profile.get("tone", ""),
        "cta": profile.get("cta", ""),
        "themes": profile.get("themes", ""),
        "forbidden": profile.get("forbidden", ""),
        "extra": profile.get("extra", ""),
        "links": "",
    }

    return templates.TemplateResponse(
        request,
        "projects/_profile_fragment.html",
        {
            "project": project_data,
            "ai_generated": True,
            "llm_error": None,
            "step1": step1,
        },
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
                "show_full": True,
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
        {"project": dict(project), "errors": [], "show_full": True},
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
                "show_full": True,
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
    # telegram
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    # vk
    access_token: str = Form(""),
    group_id: str = Form(""),
    # youtube
    client_id: str = Form(""),
    client_secret: str = Form(""),
    refresh_token: str = Form(""),
    # instagram / dzen
    note: str = Form(""),
    # legacy compat fields (старые сохранения не ломаем)
    channel_id: str = Form(""),
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

    # ── Собрать config JSON ───────────────────────────────────────────────────
    # Для telegram: chat_id переопределяет legacy channel_id
    tg_channel = chat_id.strip() or channel_id.strip()
    config_map = {
        "telegram": {"channel_id": tg_channel},
        "vk": {"group_id": group_id.strip()},
        "youtube": {"channel": channel.strip()},
        "instagram": {"account": account_name.strip()},
        "dzen": {"account": account_name.strip()},
    }
    config_json = json.dumps(config_map.get(platform, {}), ensure_ascii=False)

    enabled_int = 1 if enabled in ("1", "on", "true") else 0

    # ── Собрать credentials JSON по платформе ────────────────────────────────
    def _build_new_credentials() -> str | None:
        """Возвращает зашифрованный JSON или None, если нет данных."""
        if platform == "telegram":
            token = bot_token.strip() or credentials_token.strip()
            if not token:
                return None
            return encrypt(json.dumps({"bot_token": token, "chat_id": tg_channel},
                                      ensure_ascii=False))
        elif platform == "vk":
            token = access_token.strip() or credentials_token.strip()
            if not token:
                return None
            return encrypt(json.dumps({"access_token": token}, ensure_ascii=False))
        elif platform == "youtube":
            cid = client_id.strip()
            csec = client_secret.strip()
            rtoken = refresh_token.strip() or credentials_token.strip()
            if not (cid or csec or rtoken):
                return None
            return encrypt(json.dumps({
                "client_id": cid,
                "client_secret": csec,
                "refresh_token": rtoken,
            }, ensure_ascii=False))
        else:
            # instagram / dzen — без обязательных credentials
            token = credentials_token.strip()
            if not token:
                return None
            return encrypt(json.dumps({"token": token}, ensure_ascii=False))

    new_creds_value = _build_new_credentials()

    with get_db() as db:
        existing = db.execute(
            "SELECT id, credentials FROM project_platforms WHERE project_id=? AND platform=?",
            (project["id"], platform),
        ).fetchone()

        # Обновить credentials только если передан новый (не пустой)
        if new_creds_value is not None:
            final_credentials = new_creds_value
        else:
            # Оставить старое значение
            final_credentials = existing["credentials"] if existing else None

        if existing:
            db.execute(
                """
                UPDATE project_platforms
                SET enabled=?, mode=?, config=?, credentials=?
                WHERE id=?
                """,
                (enabled_int, mode, config_json, final_credentials, existing["id"]),
            )
        else:
            db.execute(
                """
                INSERT INTO project_platforms
                    (project_id, platform, enabled, mode, config, credentials)
                VALUES (?,?,?,?,?,?)
                """,
                (project["id"], platform, enabled_int, mode, config_json, final_credentials),
            )

    return RedirectResponse(f"/projects/{slug}", status_code=303)


# ─── Проверка подключения площадки (HTMX) ────────────────────────────────────

_CHECK_OK = (
    '<span style="color:var(--green);font-weight:600;">✓ {msg}</span>'
)
_CHECK_ERR = (
    '<span style="color:var(--red);font-weight:600;">✗ {msg}</span>'
)

_MANUAL_PLATFORMS = {"instagram", "dzen"}


def _check_html_ok(msg: str) -> HTMLResponse:
    return HTMLResponse(_CHECK_OK.format(msg=msg))


def _check_html_err(msg: str) -> HTMLResponse:
    return HTMLResponse(_CHECK_ERR.format(msg=msg))


@router.post("/{slug}/platforms/{platform}/check", response_class=HTMLResponse)
async def platform_check(slug: str, platform: str):
    """HTMX-эндпоинт: проверить подключение к площадке."""
    if platform not in PLATFORMS:
        return HTMLResponse("Неизвестная площадка", status_code=400)

    with get_db() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE slug=?", (slug,)
        ).fetchone()
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    # instagram / dzen — ручная публикация, проверка не требуется
    if platform in _MANUAL_PLATFORMS:
        return _check_html_ok("ручная публикация, проверка не требуется")

    with get_db() as db:
        row = db.execute(
            "SELECT credentials, config FROM project_platforms"
            " WHERE project_id=? AND platform=?",
            (project["id"], platform),
        ).fetchone()

    credentials: dict = {}
    if row and row["credentials"]:
        try:
            credentials = json.loads(decrypt(row["credentials"]))
        except (ValueError, Exception):
            return _check_html_err("Не удалось расшифровать credentials")

    config: dict = {}
    if row and row["config"]:
        try:
            config = json.loads(row["config"])
        except (json.JSONDecodeError, TypeError):
            config = {}

    try:
        if platform == "telegram":
            return _check_telegram(credentials, config)
        elif platform == "vk":
            return _check_vk(credentials, config)
        elif platform == "youtube":
            return _check_youtube(credentials)
    except Exception as exc:  # noqa: BLE001
        return _check_html_err(f"Ошибка: {exc}")

    return _check_html_err("Неизвестная платформа")


def _check_telegram(credentials: dict, config: dict) -> HTMLResponse:
    bot_token = credentials.get("bot_token", "")
    if not bot_token:
        return _check_html_err("bot_token не задан")
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=10,
        )
        data = resp.json()
    except httpx.HTTPError as exc:
        return _check_html_err(f"сетевая ошибка: {exc}")
    if not data.get("ok"):
        desc = data.get("description", "нет описания")
        return _check_html_err(f"Telegram API: {desc}")
    username = data.get("result", {}).get("username", "?")
    return _check_html_ok(f"бот @{username} доступен")


def _check_vk(credentials: dict, config: dict) -> HTMLResponse:
    access_token = credentials.get("access_token", "")
    group_id = config.get("group_id", "")
    if not access_token:
        return _check_html_err("access_token не задан")
    try:
        params: dict = {
            "access_token": access_token,
            "v": "5.199",
        }
        if group_id:
            params["group_id"] = group_id
        resp = httpx.get(
            "https://api.vk.com/method/groups.getById",
            params=params,
            timeout=10,
        )
        data = resp.json()
    except httpx.HTTPError as exc:
        return _check_html_err(f"сетевая ошибка: {exc}")
    if "error" in data:
        err = data["error"]
        return _check_html_err(
            f"VK API {err.get('error_code')}: {err.get('error_msg', 'ошибка')}"
        )
    groups = data.get("response", [])
    name = groups[0].get("name", "?") if groups else "группа найдена"
    return _check_html_ok(f"группа «{name}» доступна")


def _check_youtube(credentials: dict) -> HTMLResponse:
    cid = credentials.get("client_id", "")
    csec = credentials.get("client_secret", "")
    rtoken = credentials.get("refresh_token", "")
    if not (cid and csec and rtoken):
        return _check_html_err("client_id / client_secret / refresh_token не заданы")
    try:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cid,
                "client_secret": csec,
                "refresh_token": rtoken,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
    except httpx.HTTPError as exc:
        return _check_html_err(f"сетевая ошибка: {exc}")
    if resp.status_code != 200:
        try:
            body = resp.json()
            errmsg = body.get("error_description") or body.get("error") or resp.text
        except Exception:
            errmsg = resp.text
        return _check_html_err(f"OAuth ошибка: {errmsg}")
    return _check_html_ok("OAuth токен обновлён успешно")
