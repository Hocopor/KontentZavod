"""
Фабрика готового контента (этап 7, волна C).

process_factory() — фоновый джоб (тик 1 мин, подключается оркестратором в scheduler.py).
За один тик:

  A) Дожать видео: plan_items status='generating' с content_id —
       content.status='review' (рендер готов) → content 'approved', создать schedule,
                                                 item → 'generated';
       content.status='production' и produce_attempts>=3 → item 'error' (текст из produce_error).

  B) Взять В РАБОТУ ОДИН пункт: status='approved', content_id IS NULL,
       проект stage='running', settings autogen=1 и gen_paused=0, платформа enabled=1,
       date <= today + gen_lookahead_days. Сортировка date, time_slot.
       → пометить 'generating' → generate_for_item():
           text/story → создать schedule + item 'generated';
           video      → idea+content в production, item уже 'generating' (schedule позже, в A).

Schedule создаётся РОВНО ОДИН РАЗ (для text/story — здесь после generate_for_item;
для video — в блоке A, когда рендер готов). generate_for_item НЕ создаёт schedule.

Один контент за тик (генерация/LLM тяжёлые). Наружу исключений не бросаем (logger.exception).
"""
import json
import logging
from datetime import date, datetime, timedelta

from app import catalog
from app.db import get_db, get_project_settings
from app.pipeline.from_plan import generate_for_item

logger = logging.getLogger(__name__)

_PRODUCE_MAX_ATTEMPTS = 3


# ─── Вспомогательное: создание schedule ───────────────────────────────────────


def _planned_at(item_date: str, time_slot: str | None) -> str:
    """
    Собрать planned_at в формате '%Y-%m-%d %H:%M:%S' из date пункта + time_slot.
    time_slot пуст → '12:00'.
    """
    slot = (time_slot or "").strip() or "12:00"
    # Нормализуем 'HH:MM' (на всякий случай отрезаем секунды/мусор)
    hhmm = slot[:5]
    return f"{item_date} {hhmm}:00"


def _create_schedule(item, content_id: int) -> None:
    """
    Создать запись schedule для пункта плана РОВНО один раз.

    Статус: catalog publish='auto' → 'planned'; 'manual' → 'manual_pending'.
    Платформа — item.platform, planned_at — date пункта + time_slot.

    Идемпотентность: если запись для (content_id, platform) уже есть — не дублируем.
    """
    platform = item["platform"]
    info = catalog.type_info(platform, item["content_type"])
    publish = info["publish"] if info else "auto"
    status = "planned" if publish == "auto" else "manual_pending"
    planned_at = _planned_at(item["date"], item["time_slot"])

    with get_db() as db:
        exists = db.execute(
            "SELECT 1 FROM schedule WHERE content_id=? AND platform=? LIMIT 1",
            (content_id, platform),
        ).fetchone()
        if exists:
            logger.debug(
                "schedule для content_id=%d platform=%s уже есть — пропуск",
                content_id, platform,
            )
            return
        db.execute(
            "INSERT INTO schedule (content_id, platform, planned_at, status) "
            "VALUES (?, ?, ?, ?)",
            (content_id, platform, planned_at, status),
        )
    logger.info(
        "factory: schedule создан content_id=%d platform=%s at=%s status=%s",
        content_id, platform, planned_at, status,
    )


# ─── A. Дожать видео ──────────────────────────────────────────────────────────


def _finalize_videos() -> None:
    """
    Обработать пункты в status='generating' (видео): готовый рендер → schedule+generated;
    исчерпанные попытки рендера → error.
    """
    with get_db() as db:
        rows = db.execute(
            """
            SELECT pi.id   AS item_id,
                   pi.platform, pi.content_type, pi.date, pi.time_slot,
                   pi.content_id,
                   c.status AS content_status,
                   c.files  AS files
              FROM plan_items pi
              JOIN content c ON c.id = pi.content_id
             WHERE pi.status = 'generating'
               AND pi.content_id IS NOT NULL
            """,
        ).fetchall()

    for row in rows:
        content_status = row["content_status"]

        if content_status == "review":
            # Рендер готов → одобрить контент, создать schedule, пункт generated
            with get_db() as db:
                db.execute(
                    "UPDATE content SET status='approved', updated_at=datetime('now') "
                    "WHERE id=?",
                    (row["content_id"],),
                )
            _create_schedule(row, row["content_id"])
            with get_db() as db:
                db.execute(
                    "UPDATE plan_items SET status='generated', error_text=NULL, "
                    "updated_at=datetime('now') WHERE id=?",
                    (row["item_id"],),
                )
            logger.info(
                "factory: видео готово item_id=%d content_id=%d → generated",
                row["item_id"], row["content_id"],
            )

        elif content_status == "production":
            # Проверить, исчерпаны ли попытки рендера
            try:
                files = json.loads(row["files"]) if row["files"] else {}
            except (json.JSONDecodeError, TypeError):
                files = {}
            attempts = files.get("produce_attempts", 0) or 0
            if attempts >= _PRODUCE_MAX_ATTEMPTS:
                error_text = files.get("produce_error") or "Рендер видео не удался"
                with get_db() as db:
                    db.execute(
                        "UPDATE plan_items SET status='error', error_text=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (str(error_text)[:2000], row["item_id"]),
                    )
                logger.warning(
                    "factory: видео провалено item_id=%d (%d попыток) → error",
                    row["item_id"], attempts,
                )
        # прочие статусы (например снова production без исчерпания) — ждём дальше


# ─── B. Взять в работу один пункт ─────────────────────────────────────────────


def _pick_and_generate() -> None:
    """
    Выбрать один подходящий approved-пункт и сгенерировать контент.
    Для text/story — после генерации создать schedule и перевести в generated.
    Для video — generate_for_item уже перевёл в generating (schedule позже).
    """
    today = date.today().isoformat()

    # Кандидаты: approved, без контента, проект running. Settings/lookahead проверяем в Python
    # (settings — JSON в projects, lookahead-окно зависит от per-project настроек).
    with get_db() as db:
        candidates = db.execute(
            """
            SELECT pi.id AS item_id, pi.platform, pi.content_type, pi.date,
                   pi.time_slot, pi.project_id,
                   p.settings AS settings,
                   pp.enabled AS platform_enabled
              FROM plan_items pi
              JOIN projects p ON p.id = pi.project_id
              LEFT JOIN project_platforms pp
                     ON pp.project_id = pi.project_id AND pp.platform = pi.platform
             WHERE pi.status = 'approved'
               AND pi.content_id IS NULL
               AND p.stage = 'running'
             ORDER BY pi.date ASC, pi.time_slot ASC
            """,
        ).fetchall()

    chosen = None
    for row in candidates:
        if not row["platform_enabled"]:
            continue
        st = get_project_settings(row["settings"])
        if int(st.get("autogen", 0)) != 1:
            continue
        if int(st.get("gen_paused", 0)) != 0:
            continue
        lookahead = int(st.get("gen_lookahead_days", 3))
        max_date = (date.today() + timedelta(days=lookahead)).isoformat()
        if row["date"] > max_date:
            continue
        chosen = row
        break

    if chosen is None:
        logger.debug("factory: нет подходящих пунктов для генерации")
        return

    item_id = chosen["item_id"]

    # Пометить generating ДО тяжёлой генерации (защита от повторного взятия)
    with get_db() as db:
        db.execute(
            "UPDATE plan_items SET status='generating', updated_at=datetime('now') "
            "WHERE id=? AND status='approved'",
            (item_id,),
        )

    info = catalog.type_info(chosen["platform"], chosen["content_type"])
    kind = info["kind"] if info else None

    content_id = generate_for_item(item_id)

    if content_id is None:
        # generate_for_item уже выставил status='error'
        logger.warning("factory: генерация item_id=%d не удалась", item_id)
        return

    # Для text/story создаём schedule и переводим пункт в generated.
    # (generate_for_item для text/story уже поставил generated — но schedule создаёт фабрика.)
    if kind in ("text", "story"):
        # Перечитываем строку (generate_for_item обновил content_id)
        with get_db() as db:
            item = db.execute(
                "SELECT * FROM plan_items WHERE id=?", (item_id,)
            ).fetchone()
        if item is not None:
            _create_schedule(item, content_id)
    # video: schedule создаётся в _finalize_videos, когда рендер готов


# ─── Тик фабрики ──────────────────────────────────────────────────────────────


def process_factory() -> None:
    """
    Один тик фабрики: дожать готовые видео (A), затем взять в работу один пункт (B).

    Наружу исключений не бросаем — фоновый джоб не должен падать.
    """
    try:
        _finalize_videos()
    except Exception:  # noqa: BLE001
        logger.exception("process_factory: ошибка в _finalize_videos")

    try:
        _pick_and_generate()
    except Exception:  # noqa: BLE001
        logger.exception("process_factory: ошибка в _pick_and_generate")
