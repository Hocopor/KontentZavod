"""
Продакшн-конвейер видеоконтента.

Публичный API:
    produce_video(content_id)   — выполнить полную цепочку рендера для одного content
    process_production()        — взять самый старый необработанный контент и produce_video

Цепочка produce_video:
  texts["video"] → synthesize_scenes → build_ass → fetch_scene_assets → pick_music
  → _do_render (ленивый импорт render_video) → cleanup_after_render
  → content.files = {"video_path": "...", "preview_path": "...", "subs_path": "..."}
  → content.status = 'review'

Ошибка любого шага:
  → content.files["produce_error"] = <текст>, files["produce_attempts"] += 1
  → status ОСТАЁТСЯ 'production'

Пути в files JSON:
  Все пути хранятся как АБСОЛЮТНЫЕ строки (str(Path.absolute())).
  Это упрощает рендер и отдачу файлов без привязки к DATA_DIR.

process_production():
  Берёт ОДИН самый старый content (status='production', type video_*, produce_attempts<3).
  Рендер тяжёлый — за тик только один.
"""
import json
import logging
from pathlib import Path

from app.config import settings
from app.db import get_db
from app.pipeline.tts import synthesize_scenes
from app.pipeline.subtitles import build_ass
from app.pipeline.assets import fetch_scene_assets, pick_music
from app.services.cleanup import cleanup_after_render

logger = logging.getLogger(__name__)

# ─── Ленивый импорт рендера ───────────────────────────────────────────────────


def _do_render(content_id: int, scene_audios, asset_paths, subs_path, music_path):
    """
    Обёртка вокруг render_video с ленивым импортом.
    Патчим её в тестах, чтобы не зависеть от наличия render.py.

    Returns:
        (video_path, preview_path) — оба Path.
    """
    from app.pipeline.render import render_video  # noqa: PLC0415

    return render_video(content_id, scene_audios, asset_paths, subs_path, music_path)


# ─── Основная функция ─────────────────────────────────────────────────────────


def produce_video(content_id: int) -> None:
    """
    Выполнить полный продакшн-цикл для видеоконтента.

    Предусловие:
      - content.type IN ('video_footage', 'video_slideshow')
      - content.status = 'production'

    При успехе:
      - content.files = {"video_path": "...", "preview_path": "...", "subs_path": "..."}
      - content.status = 'review'

    При любой ошибке:
      - content.files["produce_error"] = <сообщение>
      - content.files["produce_attempts"] += 1
      - content.status ОСТАЁТСЯ 'production'
      - loggging.warning

    Raises:
        ValueError: если контент не найден, не видеотип или статус не 'production'.
    """
    # ── 1. Загрузить контент ──────────────────────────────────────────────────
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM content WHERE id=?", (content_id,)
        ).fetchone()

    if row is None:
        raise ValueError(f"Контент с id={content_id} не найден")

    content_type = row["type"]
    if content_type not in ("video_footage", "video_slideshow"):
        raise ValueError(
            f"Контент id={content_id} имеет тип '{content_type}', "
            "ожидается 'video_footage' или 'video_slideshow'"
        )
    if row["status"] != "production":
        raise ValueError(
            f"Контент id={content_id} имеет статус '{row['status']}', "
            "ожидается 'production'"
        )

    # ── 2. Распарсить texts и files ───────────────────────────────────────────
    try:
        texts = json.loads(row["texts"]) if row["texts"] else {}
    except (json.JSONDecodeError, TypeError):
        texts = {}

    try:
        files = json.loads(row["files"]) if row["files"] else {}
    except (json.JSONDecodeError, TypeError):
        files = {}

    video_block = texts.get("video", {})
    scenes = video_block.get("scenes", [])
    voice = video_block.get("voice", "dmitry")
    mood = video_block.get("mood", "neutral")

    scene_texts = [s["text"] for s in scenes]

    # Рабочая директория для медиафайлов этого контента
    media_dir = settings.data_dir_absolute / "media" / str(content_id)

    # ── 3. Цепочка продакшна ──────────────────────────────────────────────────
    try:
        # 3a. TTS
        logger.info("produce_video: content_id=%d → TTS (%d сцен)", content_id, len(scene_texts))
        scene_audios = synthesize_scenes(scene_texts, media_dir, voice=voice)

        # 3b. Субтитры
        all_words = []
        for sa in scene_audios:
            all_words.extend(sa.words)
        subs_path = media_dir / "subs.ass"
        build_ass(all_words, subs_path)
        logger.info("produce_video: content_id=%d → субтитры построены", content_id)

        # 3c. Ассеты
        logger.info("produce_video: content_id=%d → fetch assets", content_id)
        asset_paths = fetch_scene_assets(content_id, scenes, content_type)

        # 3d. Музыка
        music_path = pick_music(mood)
        logger.info(
            "produce_video: content_id=%d → музыка: %s",
            content_id,
            music_path,
        )

        # 3e. Рендер (ленивый импорт)
        logger.info("produce_video: content_id=%d → рендер", content_id)
        video_path, preview_path = _do_render(
            content_id, scene_audios, asset_paths, subs_path, music_path
        )

        # 3f. Очистка исходников
        cleanup_after_render(content_id)
        logger.info("produce_video: content_id=%d → очистка выполнена", content_id)

        # 3g. Сохранить пути и сменить статус
        files_out = {
            "video_path": str(video_path),
            "preview_path": str(preview_path),
            "subs_path": str(subs_path),
        }

        with get_db() as db:
            db.execute(
                """
                UPDATE content
                   SET files=?, status='review', updated_at=datetime('now')
                 WHERE id=?
                """,
                (json.dumps(files_out, ensure_ascii=False), content_id),
            )

        logger.info(
            "produce_video: content_id=%d ГОТОВО → status='review', video=%s",
            content_id,
            video_path,
        )

    except Exception as exc:  # noqa: BLE001
        # Любая ошибка — записываем и оставляем статус production
        error_text = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "produce_video: content_id=%d ОШИБКА: %s", content_id, error_text
        )

        # Инкрементируем счётчик попыток
        current_attempts = files.get("produce_attempts", 0)
        files["produce_error"] = error_text
        files["produce_attempts"] = current_attempts + 1

        with get_db() as db:
            db.execute(
                """
                UPDATE content
                   SET files=?, updated_at=datetime('now')
                 WHERE id=?
                """,
                (json.dumps(files, ensure_ascii=False), content_id),
            )


# ─── Тик планировщика ─────────────────────────────────────────────────────────


def process_production() -> None:
    """
    Взять САМЫЙ СТАРЫЙ контент (status='production', type video_*,
    produce_attempts<3) и выполнить produce_video.

    Рендер тяжёлый — за один тик обрабатываем только ОДИН контент.
    Если таких контентов нет — no-op.
    """
    with get_db() as db:
        row = db.execute(
            """
            SELECT c.id,
                   COALESCE(json_extract(c.files, '$.produce_attempts'), 0) AS attempts
              FROM content c
             WHERE c.status = 'production'
               AND c.type IN ('video_footage', 'video_slideshow')
               AND COALESCE(json_extract(c.files, '$.produce_attempts'), 0) < 3
             ORDER BY c.created_at ASC
             LIMIT 1
            """,
        ).fetchone()

    if row is None:
        logger.debug("process_production: нет контента для обработки")
        return

    content_id: int = row["id"]
    logger.info("process_production: начинаем produce_video для content_id=%d", content_id)

    try:
        produce_video(content_id)
    except ValueError as exc:
        # Контент исчез или имеет неожиданный статус — логируем и продолжаем
        logger.warning(
            "process_production: produce_video(content_id=%d) → ValueError: %s",
            content_id,
            exc,
        )
