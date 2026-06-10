"""
Генерация видео-сценария по идее.

Функция generate_video_script:
  - Загружает идею + профиль проекта + включённые площадки из БД
  - Строит промпт через app/pipeline/prompts.py (шаблон video_script)
  - Вызывает llm.chat (purpose='video_script', json_mode=True)
  - Валидирует ответ: title; video.voice/mood/scenes (непустые text и keywords list);
    features; блоки включённых площадок кроме dzen
  - Один retry при невалидном ответе, затем LLMError
  - Записывает запись в content (type=template, status='text_review',
    texts=JSON весь объект без features/title,
    features=JSON features, title=title)
  - Переводит идею в status='used'
  - Возвращает content_id
"""
import json
import logging
import re

from app.db import get_db
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt
from app.pipeline.script import _PLATFORM_SPECS

logger = logging.getLogger(__name__)

# ─── Обязательные ключи в блоке платформы для видео ──────────────────────────

_PLATFORM_REQUIRED_KEYS: dict[str, list[str]] = {
    "telegram": ["text"],
    "vk": ["text"],
    "instagram": ["caption"],
    "youtube": ["title", "description"],
    # dzen — НЕ включаем для видео
}

# Допустимые значения voice и mood
_VALID_VOICES = {"dmitry", "svetlana"}
_VALID_MOODS = {"energetic", "calm", "inspiring", "neutral"}


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _validate_video_script(data: dict, enabled_platforms: list[str]) -> list[str]:
    """
    Проверяет структуру ответа LLM для видео-сценария.
    Возвращает список строк с ошибками (пустой = всё ОК).
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Ответ должен быть JSON-объектом"]

    if not data.get("title"):
        errors.append("Отсутствует поле 'title'")

    # ── Блок video ────────────────────────────────────────────────────────────
    video = data.get("video")
    if not isinstance(video, dict):
        errors.append("Отсутствует или невалидно поле 'video'")
    else:
        if video.get("voice") not in _VALID_VOICES:
            errors.append(
                f"video.voice должен быть одним из {_VALID_VOICES}, "
                f"получено: {video.get('voice')!r}"
            )
        if video.get("mood") not in _VALID_MOODS:
            errors.append(
                f"video.mood должен быть одним из {_VALID_MOODS}, "
                f"получено: {video.get('mood')!r}"
            )
        scenes = video.get("scenes")
        if not isinstance(scenes, list) or len(scenes) == 0:
            errors.append("video.scenes должен быть непустым массивом")
        else:
            for i, scene in enumerate(scenes):
                if not isinstance(scene, dict):
                    errors.append(f"video.scenes[{i}] должна быть объектом")
                    continue
                if not scene.get("text"):
                    errors.append(f"video.scenes[{i}].text отсутствует или пуст")
                kw = scene.get("keywords")
                if not isinstance(kw, list) or len(kw) == 0:
                    errors.append(f"video.scenes[{i}].keywords должен быть непустым массивом")

    # ── Блок features ─────────────────────────────────────────────────────────
    features = data.get("features")
    if not isinstance(features, dict):
        errors.append("Отсутствует или невалидно поле 'features'")
    else:
        for key in ("hook_type", "topic", "length", "format"):
            if not features.get(key):
                errors.append(f"features.{key} отсутствует")

    # ── Блоки площадок (кроме dzen) ───────────────────────────────────────────
    platforms_to_check = [p for p in enabled_platforms if p != "dzen"]
    for platform in platforms_to_check:
        block = data.get(platform)
        if not isinstance(block, dict):
            errors.append(f"Отсутствует блок для площадки '{platform}'")
            continue
        for req_key in _PLATFORM_REQUIRED_KEYS.get(platform, []):
            if not block.get(req_key):
                errors.append(f"{platform}.{req_key} отсутствует или пуст")

    return errors


def _parse_video_script_json(raw: str, enabled_platforms: list[str]) -> dict:
    """
    Парсит и валидирует JSON-ответ видео-сценария.
    Бросает ValueError при невалидном JSON или структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    errors = _validate_video_script(data, enabled_platforms)
    if errors:
        raise ValueError("Невалидная структура ответа: " + "; ".join(errors))
    return data


# ─── Публичная функция ────────────────────────────────────────────────────────


def generate_video_script(idea_id: int, template: str) -> int:
    """
    Генерирует видео-сценарий по идее.

    Args:
        idea_id:  ID идеи в таблице ideas.
        template: 'video_footage' | 'video_slideshow'

    Returns:
        content_id — ID созданной записи в таблице content.

    Raises:
        ValueError:  Идея не найдена, проект заархивирован или невалидный template.
        LLMError:    LLM не ответил валидным JSON после retry.
    """
    if template not in ("video_footage", "video_slideshow"):
        raise ValueError(
            f"Неизвестный шаблон видео: {template!r}. "
            "Ожидается 'video_footage' или 'video_slideshow'."
        )

    # ── 1. Загрузить идею и профиль проекта ──────────────────────────────────
    with get_db() as db:
        idea = db.execute(
            "SELECT * FROM ideas WHERE id=?", (idea_id,)
        ).fetchone()

    if idea is None:
        raise ValueError(f"Идея с id={idea_id} не найдена")

    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (idea["project_id"],)
        ).fetchone()

    if project is None:
        raise ValueError(f"Проект для идеи id={idea_id} не найден")
    if project["status"] == "archived":
        raise ValueError(
            f"Проект «{project['name']}» заархивирован — генерация невозможна"
        )

    # ── 2. Включённые площадки ────────────────────────────────────────────────
    with get_db() as db:
        platform_rows = db.execute(
            "SELECT platform FROM project_platforms WHERE project_id=? AND enabled=1",
            (project["id"],),
        ).fetchall()

    enabled_platforms = [r["platform"] for r in platform_rows]
    if not enabled_platforms:
        # Если площадки не настроены — используем всё кроме dzen
        enabled_platforms = ["telegram", "vk", "youtube", "instagram"]
        logger.warning(
            "generate_video_script: у проекта %d нет включённых площадок, "
            "генерируем без dzen",
            project["id"],
        )

    # dzen для видео не используется
    enabled_platforms_for_video = [p for p in enabled_platforms if p != "dzen"]

    # ── 3. Спецификации площадок для промпта ─────────────────────────────────
    platforms_spec = "\n\n".join(
        _PLATFORM_SPECS[p]
        for p in enabled_platforms_for_video
        if p in _PLATFORM_SPECS
    )

    # ── 4. Активные learnings ─────────────────────────────────────────────────
    with get_db() as db:
        learning_rows = db.execute(
            "SELECT insight FROM learnings WHERE project_id=? AND active=1 ORDER BY weight DESC",
            (project["id"],),
        ).fetchall()

    learnings_text = (
        "\n".join(f"• {r['insight']}" for r in learning_rows)
        if learning_rows
        else "Пока нет — это первые генерации проекта."
    )

    # ── 5. Построить промпт ───────────────────────────────────────────────────
    prompt = load_prompt(
        "video_script",
        PROJECT_NAME=project["name"] or "",
        PROJECT_DESCRIPTION=project["description"] or "",
        PROJECT_AUDIENCE=project["audience"] or "",
        PROJECT_TONE=project["tone"] or "",
        PROJECT_GOALS=project["goals"] or "",
        PROJECT_CTA=project["cta"] or "",
        PROJECT_EXTRA=project["extra"] or "",
        PROJECT_LEARNINGS=learnings_text,
        IDEA_TEXT=idea["text"],
        PLATFORMS_SPEC=platforms_spec,
    )

    messages = [{"role": "user", "content": prompt}]

    # ── 6. Вызов LLM + валидация ─────────────────────────────────────────────
    raw = chat(messages, purpose="video_script", json_mode=True)
    try:
        data = _parse_video_script_json(raw, enabled_platforms_for_video)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "video_script: первая попытка парсинга провалилась (%s), retry", exc
        )
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка: {exc}. "
                    "Ответь ТОЛЬКО валидным JSON-объектом строго по формату из задания. "
                    "Обязательные ключи: title, video (voice/mood/scenes), features, "
                    f"и блоки для площадок: "
                    f"{', '.join(enabled_platforms_for_video)}."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="video_script", json_mode=True)
        try:
            data = _parse_video_script_json(raw2, enabled_platforms_for_video)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON видео-сценария после двух попыток: {exc2}"
            ) from exc2

    # ── 7. Сформировать texts — video + платформенные блоки ──────────────────
    # Хранится: video-блок + блоки включённых площадок (кроме dzen)
    texts: dict = {}
    texts["video"] = data["video"]
    for platform in enabled_platforms_for_video:
        if platform in data:
            texts[platform] = data[platform]

    texts_json = json.dumps(texts, ensure_ascii=False)
    features_json = json.dumps(data["features"], ensure_ascii=False)
    title = data.get("title", "")

    # ── 8. Записать content + обновить идею ──────────────────────────────────
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO content
                (project_id, idea_id, type, title, texts, features, status)
            VALUES (?, ?, ?, ?, ?, ?, 'text_review')
            """,
            (project["id"], idea_id, template, title, texts_json, features_json),
        )
        content_id = cur.lastrowid

        db.execute(
            "UPDATE ideas SET status='used' WHERE id=?",
            (idea_id,),
        )

    logger.info(
        "generate_video_script: idea_id=%d → content_id=%d, template=%s, площадки=%s",
        idea_id,
        content_id,
        template,
        enabled_platforms_for_video,
    )
    return content_id
