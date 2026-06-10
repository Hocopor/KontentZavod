"""
Генерация готового контента из одобренного пункта контент-плана (этап 7, волна C).

generate_for_item(item_id) — диспетчер по kind типа контента (из catalog.type_info):
  - text  → пост/статья (purpose='item_post')   → content type='post'|'article', status='approved'
  - story → история с картинкой (purpose='item_story', Pollinations) → content type='story'
  - video → idea + generate_video_script + статус 'production' (рендерит process_production)

При успехе создаёт строку content, проставляет plan_items.content_id и статус:
  text/story → 'generated'; video → 'generating' (станет 'generated', когда видео отрендерится).
Schedule создаёт фабрика (services/factory.py), НЕ эта функция.

При ошибке: plan_items.status='error', error_text; возвращает None (наружу не бросает).
"""
import json
import logging
import re

from app import catalog
from app.db import get_db
from app.llm import chat, LLMError
from app.pipeline.assets import _fetch_pollinations_image, _fake_image
from app.pipeline.prompts import load_prompt
from app.pipeline.script import _PLATFORM_SPECS
from app.pipeline.video_script import generate_video_script
from app.config import settings

logger = logging.getLogger(__name__)


# ─── Парсинг JSON ─────────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_item_post(raw: str) -> dict:
    """Парсит и валидирует JSON поста. Бросает ValueError при невалидной структуре."""
    data = json.loads(_strip_fences(raw))
    if not isinstance(data, dict):
        raise ValueError("Ответ должен быть JSON-объектом")
    if not data.get("text"):
        raise ValueError("Отсутствует поле 'text'")
    if not isinstance(data.get("features"), dict):
        raise ValueError("Отсутствует или невалидно поле 'features'")
    return data


def _parse_item_story(raw: str) -> dict:
    """Парсит и валидирует JSON истории. Бросает ValueError при невалидной структуре."""
    data = json.loads(_strip_fences(raw))
    if not isinstance(data, dict):
        raise ValueError("Ответ должен быть JSON-объектом")
    if not data.get("image_prompt"):
        raise ValueError("Отсутствует поле 'image_prompt'")
    if not data.get("caption"):
        raise ValueError("Отсутствует поле 'caption'")
    if not isinstance(data.get("features"), dict):
        raise ValueError("Отсутствует или невалидно поле 'features'")
    return data


# ─── Сбор контекста ───────────────────────────────────────────────────────────


def _strategy_block(db, project_id: int, platform: str) -> str:
    """
    Текстовое описание стратегии для платформы из активной стратегии проекта.
    Пустая строка, если активной стратегии нет или платформы в ней нет.
    """
    row = db.execute(
        "SELECT strategy FROM strategies WHERE project_id=? AND status='active' "
        "ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None or not row["strategy"]:
        return "Стратегия пока не задана — опирайся на профиль проекта."
    try:
        strat = json.loads(row["strategy"])
    except (json.JSONDecodeError, TypeError):
        return "Стратегия пока не задана — опирайся на профиль проекта."

    pdata = (strat.get("platforms") or {}).get(platform)
    parts = []
    summary = (strat.get("summary") or "").strip()
    if summary:
        parts.append(f"Общее: {summary}")
    if isinstance(pdata, dict):
        if pdata.get("goals"):
            parts.append(f"Цели площадки: {pdata['goals']}")
        rubrics = pdata.get("rubrics")
        if isinstance(rubrics, list) and rubrics:
            parts.append("Рубрики: " + "; ".join(str(r) for r in rubrics))
        if pdata.get("kpi"):
            parts.append(f"KPI: {pdata['kpi']}")
    return "\n".join(parts) if parts else "Стратегия пока не задана — опирайся на профиль проекта."


def _learnings_text(db, project_id: int) -> str:
    rows = db.execute(
        "SELECT insight FROM learnings WHERE project_id=? AND active=1 ORDER BY weight DESC",
        (project_id,),
    ).fetchall()
    if not rows:
        return "Пока нет — это первые генерации проекта."
    return "\n".join(f"• {r['insight']}" for r in rows)


def _brief_fields(brief_json: str | None) -> dict:
    """Распарсить brief JSON пункта плана → плоские поля для промпта."""
    try:
        brief = json.loads(brief_json) if brief_json else {}
    except (json.JSONDecodeError, TypeError):
        brief = {}
    if not isinstance(brief, dict):
        brief = {}
    keywords = brief.get("keywords")
    if isinstance(keywords, list):
        keywords_str = ", ".join(str(k) for k in keywords)
    else:
        keywords_str = str(keywords or "")
    return {
        "hook": str(brief.get("hook") or ""),
        "outline": str(brief.get("outline") or ""),
        "cta": str(brief.get("cta") or ""),
        "keywords": keywords_str,
        "keywords_list": keywords if isinstance(keywords, list) else [],
        "rubric": str(brief.get("rubric") or ""),
    }


# ─── Установка статуса ошибки ─────────────────────────────────────────────────


def _set_item_error(item_id: int, message: str) -> None:
    """Перевести пункт плана в status='error' с текстом ошибки."""
    try:
        with get_db() as db:
            db.execute(
                "UPDATE plan_items SET status='error', error_text=?, "
                "updated_at=datetime('now') WHERE id=?",
                (message[:2000], item_id),
            )
    except Exception:  # noqa: BLE001 — даже запись ошибки не должна валить джоб
        logger.exception("Не удалось записать error для plan_item id=%d", item_id)


# ─── Генераторы по kind ───────────────────────────────────────────────────────


def _chat_with_retry(messages, purpose, parse_fn):
    """Вызов LLM + 1 retry при невалидном JSON. Бросает LLMError после второй попытки."""
    raw = chat(messages, purpose=purpose, json_mode=True)
    try:
        return parse_fn(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("%s: первая попытка парсинга провалилась (%s), retry", purpose, exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка: {exc}. Ответь ТОЛЬКО валидным JSON-объектом строго "
                    "по формату из задания, без пояснений и без markdown-фенсов."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose=purpose, json_mode=True)
        try:
            return parse_fn(raw2)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON ({purpose}) после двух попыток: {exc2}"
            ) from exc2


def _generate_text(db, item, project, brief) -> int:
    """Создать текстовый пост/статью. Возвращает content_id."""
    platform = item["platform"]
    content_type = item["content_type"]

    prompt = load_prompt(
        "item_post",
        PLATFORM=platform,
        PROJECT_NAME=project["name"] or "",
        PROJECT_DESCRIPTION=project["description"] or "",
        PROJECT_AUDIENCE=project["audience"] or "",
        PROJECT_TONE=project["tone"] or "",
        PROJECT_GOALS=project["goals"] or "",
        PROJECT_CTA=project["cta"] or "",
        PROJECT_EXTRA=project["extra"] or "",
        STRATEGY_BLOCK=_strategy_block(db, project["id"], platform),
        ITEM_TITLE=item["title"] or "",
        ITEM_HOOK=brief["hook"],
        ITEM_OUTLINE=brief["outline"],
        ITEM_CTA=brief["cta"],
        ITEM_KEYWORDS=brief["keywords"],
        ITEM_RUBRIC=brief["rubric"],
        PROJECT_LEARNINGS=_learnings_text(db, project["id"]),
        PLATFORM_SPEC=_PLATFORM_SPECS.get(platform, ""),
    )

    data = _chat_with_retry(
        [{"role": "user", "content": prompt}], "item_post", _parse_item_post
    )

    title = data.get("title") or item["title"] or ""
    text = data["text"]
    hashtags = data.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = []

    # texts в формате существующих паблишеров — только секция своей платформы
    if platform == "telegram":
        texts = {"telegram": {"text": text, "hashtags": hashtags}}
    elif platform == "vk":
        texts = {"vk": {"text": text, "hashtags": hashtags}}
    elif platform == "instagram":
        texts = {"instagram": {"caption": text, "hashtags": hashtags}}
    elif platform == "dzen":
        texts = {"dzen": {"title": title, "text": text}}
    else:
        texts = {platform: {"text": text, "hashtags": hashtags}}

    content_db_type = "article" if content_type == "article" else "post"

    with get_db() as wdb:
        cur = wdb.execute(
            """
            INSERT INTO content
                (project_id, type, title, texts, features, status)
            VALUES (?, ?, ?, ?, ?, 'approved')
            """,
            (
                project["id"],
                content_db_type,
                title,
                json.dumps(texts, ensure_ascii=False),
                json.dumps(data["features"], ensure_ascii=False),
            ),
        )
        return cur.lastrowid


def _generate_story(db, item, project, brief) -> int:
    """Создать историю с картинкой Pollinations. Возвращает content_id."""
    platform = item["platform"]

    prompt = load_prompt(
        "item_story",
        PLATFORM=platform,
        PROJECT_NAME=project["name"] or "",
        PROJECT_DESCRIPTION=project["description"] or "",
        PROJECT_AUDIENCE=project["audience"] or "",
        PROJECT_TONE=project["tone"] or "",
        PROJECT_GOALS=project["goals"] or "",
        PROJECT_CTA=project["cta"] or "",
        PROJECT_EXTRA=project["extra"] or "",
        STRATEGY_BLOCK=_strategy_block(db, project["id"], platform),
        ITEM_TITLE=item["title"] or "",
        ITEM_HOOK=brief["hook"],
        ITEM_OUTLINE=brief["outline"],
        ITEM_CTA=brief["cta"],
        ITEM_KEYWORDS=brief["keywords"],
        ITEM_RUBRIC=brief["rubric"],
        PROJECT_LEARNINGS=_learnings_text(db, project["id"]),
    )

    data = _chat_with_retry(
        [{"role": "user", "content": prompt}], "item_story", _parse_item_story
    )

    title = data.get("title") or item["title"] or ""
    image_prompt = data["image_prompt"]
    overlay_text = data.get("overlay_text") or ""
    caption = data["caption"]

    # ── Создать content СНАЧАЛА (нужен content_id для пути картинки) ──────────
    texts = {platform: {"caption": caption, "overlay_text": overlay_text}}
    with get_db() as wdb:
        cur = wdb.execute(
            """
            INSERT INTO content
                (project_id, type, title, texts, features, status)
            VALUES (?, 'story', ?, ?, ?, 'approved')
            """,
            (
                project["id"],
                title,
                json.dumps(texts, ensure_ascii=False),
                json.dumps(data["features"], ensure_ascii=False),
            ),
        )
        content_id = cur.lastrowid

    # ── Картинка Pollinations (или плейсхолдер при FAKE_ASSETS) ──────────────
    media_dir = settings.data_dir_absolute / "media" / str(content_id)
    media_dir.mkdir(parents=True, exist_ok=True)
    image_path = media_dir / "story.jpg"

    if settings.FAKE_ASSETS:
        _fake_image(image_path)
    else:
        ok = _fetch_pollinations_image([image_prompt], image_path)
        if not ok:
            raise RuntimeError("Не удалось сгенерировать картинку истории (Pollinations)")

    files = {"image_path": str(image_path.absolute())}
    with get_db() as wdb:
        wdb.execute(
            "UPDATE content SET files=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(files, ensure_ascii=False), content_id),
        )

    return content_id


def _generate_video(db, item, project, brief) -> int:
    """
    Создать idea + video-сценарий, перевести content в 'production'.
    Возвращает content_id. Рендер сделает process_production.
    """
    # Текст идеи: заголовок + краткий outline из brief
    idea_text = (item["title"] or "").strip()
    if brief["outline"]:
        idea_text = f"{idea_text}. {brief['outline']}".strip(". ")

    with get_db() as wdb:
        cur = wdb.execute(
            "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
            (project["id"], idea_text or "Видео по контент-плану", "generated", "used"),
        )
        idea_id = cur.lastrowid

    # Видео-сценарий (создаёт content type=template, status='text_review')
    content_id = generate_video_script(idea_id, template="video_footage")

    # План одобрен — минуем text_review, сразу в продакшн
    with get_db() as wdb:
        wdb.execute(
            "UPDATE content SET status='production', updated_at=datetime('now') WHERE id=?",
            (content_id,),
        )

    return content_id


# ─── Публичная функция ────────────────────────────────────────────────────────


def generate_for_item(item_id: int) -> int | None:
    """
    Создать content для одобренного пункта контент-плана.

    Args:
        item_id: ID строки plan_items.

    Returns:
        content_id созданного контента, либо None при ошибке (в этом случае
        plan_items.status='error', error_text заполнен).

    Не бросает наружу: любая ошибка → status='error', None.
    """
    try:
        with get_db() as db:
            item = db.execute(
                "SELECT * FROM plan_items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                logger.warning("generate_for_item: plan_item id=%d не найден", item_id)
                return None
            project = db.execute(
                "SELECT * FROM projects WHERE id=?", (item["project_id"],)
            ).fetchone()
            if project is None:
                _set_item_error(item_id, "Проект не найден")
                return None

            info = catalog.type_info(item["platform"], item["content_type"])
            if info is None:
                _set_item_error(
                    item_id,
                    f"Неизвестный тип '{item['content_type']}' для платформы "
                    f"'{item['platform']}'",
                )
                return None
            kind = info["kind"]
            brief = _brief_fields(item["brief"])

            if kind == "text":
                content_id = _generate_text(db, item, project, brief)
                new_status = "generated"
            elif kind == "story":
                content_id = _generate_story(db, item, project, brief)
                new_status = "generated"
            elif kind == "video":
                content_id = _generate_video(db, item, project, brief)
                new_status = "generating"
            else:
                _set_item_error(item_id, f"Неподдерживаемый kind '{kind}'")
                return None

        # Привязать content к пункту плана и обновить статус
        with get_db() as db:
            db.execute(
                "UPDATE plan_items SET content_id=?, status=?, error_text=NULL, "
                "updated_at=datetime('now') WHERE id=?",
                (content_id, new_status, item_id),
            )

        logger.info(
            "generate_for_item: item_id=%d → content_id=%d (kind=%s, status=%s)",
            item_id, content_id, kind, new_status,
        )
        return content_id

    except LLMError as exc:
        logger.warning("generate_for_item: item_id=%d LLMError: %s", item_id, exc)
        _set_item_error(item_id, f"Ошибка LLM: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — фабрика не должна падать
        logger.exception("generate_for_item: item_id=%d неожиданная ошибка", item_id)
        _set_item_error(item_id, f"Неожиданная ошибка: {exc}")
        return None
