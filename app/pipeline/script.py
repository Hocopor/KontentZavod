"""
Генерация текстового контента (поста) по идее.

Функция generate_script:
  - Загружает идею + профиль проекта + включённые площадки из БД
  - Строит промпт через app/pipeline/prompts.py
  - Вызывает llm.chat (purpose='script', json_mode=True)
  - Валидирует ответ: title, features, блок для каждой включённой площадки
  - Один retry при невалидном ответе, затем LLMError
  - Записывает запись в content (type='post', status='text_review')
  - Переводит идею в status='used'
  - Возвращает content_id
"""
import json
import logging
import re

from app.db import get_db
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt

logger = logging.getLogger(__name__)

# Спецификации площадок для промпта
_PLATFORM_SPECS: dict[str, str] = {
    "telegram": (
        "**Telegram** — до 4096 символов, поддерживает Markdown-форматирование "
        "(*жирный*, _курсив_, `код`). Эмодзи уместны умеренно. "
        "Хэштеги в конце или по тексту."
    ),
    "vk": (
        "**ВКонтакте** — оптимально до 2200 символов (длиннее обрезается в ленте). "
        "Хэштеги строго в конце поста. Эмодзи допустимы, но не перегружать."
    ),
    "instagram": (
        "**Instagram** — caption до 2200 символов, но первые 125 символов видны "
        "без нажатия «ещё» — ХУК ОБЯЗАН БЫТЬ В ПЕРВЫХ 125 СИМВОЛАХ. "
        "Хэштеги в конце, до 30 штук."
    ),
    "youtube": (
        "**YouTube Shorts** — title до 100 символов (SEO-ключи в начале). "
        "Description: первые 100 символов видны без раскрытия, "
        "основная информация + ссылки + хэштеги."
    ),
    "dzen": (
        "**Яндекс.Дзен** — лонгрид-стиль. Заголовок (title) до 70 символов — "
        "привлекательный и кликабельный. Текст (text) в Markdown: "
        "подзаголовки ##, абзацы, списки. Объём от 1500 символов для хорошего ранжирования."
    ),
}

# Обязательные ключи в блоке площадки
_PLATFORM_REQUIRED_KEYS: dict[str, list[str]] = {
    "telegram": ["text"],
    "vk": ["text"],
    "instagram": ["caption"],
    "youtube": ["title", "description"],
    "dzen": ["title", "text"],
}


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _validate_script_ab(data: dict, enabled_platforms: list[str]) -> list[str]:
    """
    Проверяет структуру A/B-ответа LLM.
    Возвращает список строк с ошибками (пустой = всё ОК).
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Ответ должен быть JSON-объектом"]

    if not data.get("title"):
        errors.append("Отсутствует поле 'title'")

    features = data.get("features")
    if not isinstance(features, dict):
        errors.append("Отсутствует или невалидно поле 'features'")
    else:
        for key in ("topic", "length", "format"):
            if not features.get(key):
                errors.append(f"features.{key} отсутствует")

    variants = data.get("variants")
    if not isinstance(variants, dict):
        errors.append("Отсутствует поле 'variants'")
        return errors

    for variant_key in ("A", "B"):
        variant = variants.get(variant_key)
        if not isinstance(variant, dict):
            errors.append(f"variants.{variant_key} отсутствует или не объект")
            continue
        if not variant.get("hook_type"):
            errors.append(f"variants.{variant_key}.hook_type отсутствует")
        for platform in enabled_platforms:
            block = variant.get(platform)
            if not isinstance(block, dict):
                errors.append(f"variants.{variant_key}: отсутствует блок для площадки '{platform}'")
                continue
            for req_key in _PLATFORM_REQUIRED_KEYS.get(platform, []):
                if not block.get(req_key):
                    errors.append(f"variants.{variant_key}.{platform}.{req_key} отсутствует или пуст")

    # hook_type вариантов должны различаться
    if isinstance(variants.get("A"), dict) and isinstance(variants.get("B"), dict):
        hook_a = variants["A"].get("hook_type")
        hook_b = variants["B"].get("hook_type")
        if hook_a and hook_b and hook_a == hook_b:
            errors.append("hook_type вариантов A и B должны различаться")

    return errors


def _validate_script(data: dict, enabled_platforms: list[str]) -> list[str]:
    """
    Проверяет структуру ответа LLM.
    Возвращает список строк с ошибками (пустой = всё ОК).
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Ответ должен быть JSON-объектом"]

    if not data.get("title"):
        errors.append("Отсутствует поле 'title'")

    features = data.get("features")
    if not isinstance(features, dict):
        errors.append("Отсутствует или невалидно поле 'features'")
    else:
        for key in ("hook_type", "topic", "length", "format"):
            if not features.get(key):
                errors.append(f"features.{key} отсутствует")

    for platform in enabled_platforms:
        block = data.get(platform)
        if not isinstance(block, dict):
            errors.append(f"Отсутствует блок для площадки '{platform}'")
            continue
        for req_key in _PLATFORM_REQUIRED_KEYS.get(platform, []):
            if not block.get(req_key):
                errors.append(f"{platform}.{req_key} отсутствует или пуст")

    return errors


def _parse_script_json(raw: str, enabled_platforms: list[str]) -> dict:
    """
    Парсит и валидирует JSON-ответ сценария.
    Бросает ValueError при невалидном JSON или структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    errors = _validate_script(data, enabled_platforms)
    if errors:
        raise ValueError("Невалидная структура ответа: " + "; ".join(errors))
    return data


def generate_script(idea_id: int) -> int:
    """
    Генерирует текстовый контент (пост) по идее.

    Args:
        idea_id: ID идеи в таблице ideas.

    Returns:
        content_id — ID созданной записи в таблице content.

    Raises:
        ValueError:  Идея не найдена или проект заархивирован.
        LLMError:    LLM не ответил валидным JSON после retry.
    """
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
        raise ValueError(f"Проект «{project['name']}» заархивирован — генерация невозможна")

    # ── 2. Включённые площадки ────────────────────────────────────────────────
    with get_db() as db:
        platform_rows = db.execute(
            "SELECT platform FROM project_platforms WHERE project_id=? AND enabled=1",
            (project["id"],),
        ).fetchall()

    enabled_platforms = [r["platform"] for r in platform_rows]
    if not enabled_platforms:
        # Если площадки не настроены — генерируем для всех (режим по умолчанию)
        enabled_platforms = ["telegram", "vk", "youtube", "instagram", "dzen"]
        logger.warning(
            "generate_script: у проекта %d нет включённых площадок, "
            "генерируем для всех",
            project["id"],
        )

    # ── 3. Спецификации площадок для промпта ─────────────────────────────────
    platforms_spec = "\n\n".join(
        _PLATFORM_SPECS[p] for p in enabled_platforms if p in _PLATFORM_SPECS
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
        "script",
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
    raw = chat(messages, purpose="script", json_mode=True)
    try:
        data = _parse_script_json(raw, enabled_platforms)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("script: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка: {exc}. "
                    "Ответь ТОЛЬКО валидным JSON-объектом строго по формату из задания. "
                    f"Обязательные ключи: title, features, и блоки для площадок: "
                    f"{', '.join(enabled_platforms)}."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="script", json_mode=True)
        try:
            data = _parse_script_json(raw2, enabled_platforms)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON сценария после двух попыток: {exc2}"
            ) from exc2

    # ── 7. Сформировать texts — только включённые площадки ───────────────────
    texts: dict[str, dict] = {}
    for platform in enabled_platforms:
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
            VALUES (?, ?, 'post', ?, ?, ?, 'text_review')
            """,
            (project["id"], idea_id, title, texts_json, features_json),
        )
        content_id = cur.lastrowid

        db.execute(
            "UPDATE ideas SET status='used' WHERE id=?",
            (idea_id,),
        )

    logger.info(
        "generate_script: idea_id=%d → content_id=%d, площадки=%s",
        idea_id,
        content_id,
        enabled_platforms,
    )
    return content_id


def generate_script_ab(idea_id: int) -> tuple[int, int]:
    """
    Генерирует два варианта текстового поста (A/B-тест хука) по одной идее.

    Args:
        idea_id: ID идеи в таблице ideas.

    Returns:
        (content_id_a, content_id_b) — ID двух созданных записей в таблице content.

    Raises:
        ValueError:  Идея не найдена или проект заархивирован.
        LLMError:    LLM не ответил валидным JSON после retry.
    """
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
        raise ValueError(f"Проект «{project['name']}» заархивирован — генерация невозможна")

    # ── 2. Включённые площадки ────────────────────────────────────────────────
    with get_db() as db:
        platform_rows = db.execute(
            "SELECT platform FROM project_platforms WHERE project_id=? AND enabled=1",
            (project["id"],),
        ).fetchall()

    enabled_platforms = [r["platform"] for r in platform_rows]
    if not enabled_platforms:
        enabled_platforms = ["telegram", "vk", "youtube", "instagram", "dzen"]
        logger.warning(
            "generate_script_ab: у проекта %d нет включённых площадок, "
            "генерируем для всех",
            project["id"],
        )

    # ── 3. Спецификации площадок для промпта ─────────────────────────────────
    platforms_spec = "\n\n".join(
        _PLATFORM_SPECS[p] for p in enabled_platforms if p in _PLATFORM_SPECS
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
        "script_ab",
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
    raw = chat(messages, purpose="script_ab", json_mode=True)
    try:
        data = _parse_script_ab_json(raw, enabled_platforms)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("script_ab: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка: {exc}. "
                    "Ответь ТОЛЬКО валидным JSON-объектом строго по формату из задания. "
                    f"Обязательные ключи: title, features, variants (с подразделами A и B). "
                    f"Каждый вариант содержит hook_type и блоки для площадок: "
                    f"{', '.join(enabled_platforms)}. "
                    "hook_type вариантов A и B ДОЛЖНЫ быть разными."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="script_ab", json_mode=True)
        try:
            data = _parse_script_ab_json(raw2, enabled_platforms)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON A/B-сценария после двух попыток: {exc2}"
            ) from exc2

    # ── 7. Сформировать тексты для каждого варианта ──────────────────────────
    title = data.get("title", "")
    base_features = data["features"]
    variants = data["variants"]

    results = []
    for variant_key in ("A", "B"):
        variant = variants[variant_key]
        hook_type = variant["hook_type"]

        texts: dict[str, dict] = {}
        for platform in enabled_platforms:
            if platform in variant:
                texts[platform] = variant[platform]

        texts_json = json.dumps(texts, ensure_ascii=False)
        features = dict(base_features)
        features["hook_type"] = hook_type
        features["ab_variant"] = variant_key
        features_json = json.dumps(features, ensure_ascii=False)
        variant_title = f"{title} [{variant_key}]"

        with get_db() as db:
            cur = db.execute(
                """
                INSERT INTO content
                    (project_id, idea_id, type, title, texts, features, status)
                VALUES (?, ?, 'post', ?, ?, ?, 'text_review')
                """,
                (project["id"], idea_id, variant_title, texts_json, features_json),
            )
            results.append(cur.lastrowid)

    # ── 8. Обновить статус идеи ───────────────────────────────────────────────
    with get_db() as db:
        db.execute(
            "UPDATE ideas SET status='used' WHERE id=?",
            (idea_id,),
        )

    content_id_a, content_id_b = results[0], results[1]
    logger.info(
        "generate_script_ab: idea_id=%d → content_id_a=%d, content_id_b=%d, площадки=%s",
        idea_id,
        content_id_a,
        content_id_b,
        enabled_platforms,
    )
    return content_id_a, content_id_b


def _parse_script_ab_json(raw: str, enabled_platforms: list[str]) -> dict:
    """
    Парсит и валидирует JSON-ответ A/B-сценария.
    Бросает ValueError при невалидном JSON или структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    errors = _validate_script_ab(data, enabled_platforms)
    if errors:
        raise ValueError("Невалидная структура A/B-ответа: " + "; ".join(errors))
    return data
