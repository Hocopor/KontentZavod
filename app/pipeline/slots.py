"""
Детерминированная генерация слотов контент-плана (этап 8.2, волна C).

Количество публикаций задаёт КОД (по активной стратегии), а не LLM. Модуль
строит «слоты» — конкретные ячейки {date, time_slot, content_type, goal} —
исходя из mix фаз стратегии, директив-override и goal_share. LLM в планнере
лишь наполняет каждый слот темой и брифом.

Ключевые понятия:
  • Якорь фаз (anchor) — strategy.activated_on (или фоллбэк created_at/today).
  • Неделя k = [anchor+7k; anchor+7k+6]; фаза недели определяется по накопленной
    сумме weeks фаз от якоря (за пределами — последняя фаза, steady state).
  • Легаси-стратегии (без phases, со старым content_mix) трактуются как одна
    бессрочная фаза — слой совместимости внутри build_slots.
"""
import json
import logging
from datetime import date, timedelta

from app import catalog

logger = logging.getLogger(__name__)

# goal_share синтезируемой легаси-фазы (как в контракте 8.2)
_LEGACY_GOAL_SHARE = {"attract": 40, "retain": 50, "sell": 10}


# ─── Хелпер недели ────────────────────────────────────────────────────────────


def week_index(anchor: date, d: date) -> int:
    """
    Индекс недели даты d относительно якоря anchor.

    Неделя k охватывает [anchor+7k; anchor+7k+6]. Для дат раньше якоря
    возвращается 0 (k<0 → 0).
    """
    k = (d - anchor).days // 7
    return k if k > 0 else 0


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _resolve_anchor(strategy_json: dict, created_at: str | None) -> date:
    """
    Якорь фаз: activated_on → date-часть created_at → today.
    """
    raw = strategy_json.get("activated_on")
    if isinstance(raw, str) and raw.strip():
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            pass
    if created_at:
        # created_at вида 'YYYY-MM-DD HH:MM:SS' — берём date-часть
        date_part = str(created_at).split(" ")[0].split("T")[0]
        try:
            return date.fromisoformat(date_part)
        except ValueError:
            pass
    return date.today()


def _synth_legacy_phase(strategy_json: dict) -> dict:
    """
    Синтез одной бессрочной фазы из легаси-стратегии (без phases).
    mix берём из platforms.<p>.content_mix.
    """
    platforms = strategy_json.get("platforms", {})
    mix: dict = {}
    if isinstance(platforms, dict):
        for p, pdata in platforms.items():
            if isinstance(pdata, dict):
                cm = pdata.get("content_mix", {})
                if isinstance(cm, dict) and cm:
                    mix[p] = dict(cm)
    return {
        "n": 1,
        "weeks": 10 ** 6,
        "goal_share": dict(_LEGACY_GOAL_SHARE),
        "mix": mix,
    }


def _phase_for_week(phases: list[dict], k: int) -> dict:
    """
    Фаза недели k по накопленной сумме weeks фаз. За пределами → последняя фаза.
    """
    cumulative = 0
    for ph in phases:
        try:
            weeks = int(ph.get("weeks", 1))
        except (TypeError, ValueError):
            weeks = 1
        if weeks < 1:
            weeks = 1
        cumulative += weeks
        if k < cumulative:
            return ph
    return phases[-1]


def _best_times(strategy_json: dict, platform: str) -> list[str]:
    """best_times платформы из стратегии (пусто → ['12:00'])."""
    platforms = strategy_json.get("platforms", {})
    pdata = platforms.get(platform) if isinstance(platforms, dict) else None
    if isinstance(pdata, dict):
        bt = pdata.get("best_times")
        if isinstance(bt, list):
            clean = [str(t).strip() for t in bt if str(t).strip()]
            if clean:
                return clean
    return ["12:00"]


def _phase_mix_for_platform(
    phase: dict,
    platform: str,
    enabled_types: set[str],
    only_content_type: str | None,
) -> dict[str, int]:
    """
    mix платформы для фазы: только включённые и валидные типы.
    only_content_type → оставить только его.
    """
    raw = phase.get("mix", {})
    pmix = raw.get(platform, {}) if isinstance(raw, dict) else {}
    out: dict[str, int] = {}
    if isinstance(pmix, dict):
        for ctype, n in pmix.items():
            if ctype not in enabled_types:
                continue
            if not catalog.is_valid(platform, ctype):
                continue
            try:
                num = int(n)
            except (TypeError, ValueError):
                continue
            if num > 0:
                out[ctype] = num
    if only_content_type is not None:
        out = {only_content_type: out[only_content_type]} if only_content_type in out else {}
    return out


def _apply_directives(
    mix: dict[str, int],
    directives: list[dict],
    platform: str,
    enabled_types: set[str],
    only_content_type: str | None,
) -> dict[str, int]:
    """
    Применить директивы-override поверх mix (в порядке id — поздние побеждают).

    directives: список {"id", "parsed": dict|None} активных директив.
    Правила override:
      • parsed.platform задан и != platform → пропустить директиву;
      • parsed.content_type задан → override только этого типа (если включён);
      • content_type null → override ВСЕХ включённых типов платформы.
    """
    result = dict(mix)
    for d in directives:
        parsed = d.get("parsed")
        if not isinstance(parsed, dict):
            continue
        per_week = parsed.get("per_week")
        try:
            per_week = int(per_week)
        except (TypeError, ValueError):
            continue
        if per_week < 0:
            continue

        d_platform = parsed.get("platform")
        if d_platform and d_platform != platform:
            continue

        d_ctype = parsed.get("content_type")
        if d_ctype:
            if d_ctype not in enabled_types or not catalog.is_valid(platform, d_ctype):
                continue
            if only_content_type is not None and d_ctype != only_content_type:
                continue
            if per_week > 0:
                result[d_ctype] = per_week
            else:
                result.pop(d_ctype, None)
        else:
            # override всех включённых типов платформы
            targets = enabled_types
            if only_content_type is not None:
                targets = {only_content_type} & enabled_types
            for ctype in targets:
                if not catalog.is_valid(platform, ctype):
                    continue
                if per_week > 0:
                    result[ctype] = per_week
                else:
                    result.pop(ctype, None)
    return result


def _distribute_goals(count: int, goal_share: dict) -> tuple[list[str], dict]:
    """
    Распределить count слотов по целям методом наибольших остатков.
    Возвращает (список целей с ненулевой квотой по убыванию квоты, словарь квот).
    """
    if count <= 0:
        return [], {}
    # нормализуем goal_share к валидным целям с положительной долей
    shares: dict[str, float] = {}
    for k, v in (goal_share or {}).items():
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num > 0:
            shares[k] = num
    if not shares:
        shares = {"attract": 1.0}

    total = sum(shares.values())
    # сырое количество на цель
    raw = {k: count * v / total for k, v in shares.items()}
    quotas = {k: int(v) for k, v in raw.items()}
    assigned = sum(quotas.values())
    remainder = count - assigned
    # раздаём остаток по наибольшим дробным частям (детерминированно по ключу)
    if remainder > 0:
        order = sorted(
            shares.keys(),
            key=lambda k: (raw[k] - int(raw[k]), shares[k], k),
            reverse=True,
        )
        for i in range(remainder):
            quotas[order[i % len(order)]] += 1

    # развернуть в список: цели с ненулевой квотой, по убыванию квоты
    goals_sorted = sorted(
        [g for g in quotas if quotas[g] > 0],
        key=lambda g: (quotas[g], shares[g], g),
        reverse=True,
    )
    return goals_sorted, quotas


def _assign_goals_round_robin(n: int, goal_share: dict) -> list[str]:
    """
    Присвоить n слотам цели: квоты по наибольшим остаткам, раздача round-robin
    по целям с ненулевой квотой (детерминированно).
    """
    if n <= 0:
        return []
    goals_sorted, quotas = _distribute_goals(n, goal_share)
    if not goals_sorted:
        return ["attract"] * n
    result: list[str] = []
    remaining = dict(quotas)
    idx = 0
    while len(result) < n:
        g = goals_sorted[idx % len(goals_sorted)]
        if remaining.get(g, 0) > 0:
            result.append(g)
            remaining[g] -= 1
        idx += 1
        # страховка от зацикливания
        if idx > n * (len(goals_sorted) + 1):
            while len(result) < n:
                result.append(goals_sorted[0])
            break
    return result


def _bump_time(time_slot: str) -> str:
    """Прибавить 30 минут по модулю суток к 'HH:MM'."""
    try:
        h, m = time_slot.split(":")
        total = (int(h) * 60 + int(m) + 30) % (24 * 60)
    except (ValueError, AttributeError):
        return "12:30"
    return f"{total // 60:02d}:{total % 60:02d}"


# ─── Основная функция ─────────────────────────────────────────────────────────


def build_slots(
    db,
    project_id: int,
    platform: str,
    date_from: str,
    date_to: str,
    only_content_type: str | None = None,
) -> list[dict]:
    """
    Детерминированно построить слоты контент-плана по активной стратегии.

    Args:
        db:                открытое соединение БД.
        project_id:        ID проекта.
        platform:          ключ платформы.
        date_from:         начало периода YYYY-MM-DD (включительно).
        date_to:           конец периода YYYY-MM-DD (включительно).
        only_content_type: если задан — генерировать только для этого типа.

    Returns:
        Список слотов [{"date", "time_slot", "content_type", "goal"}],
        отсортированный по (date, time_slot). Пустой при отсутствии стратегии,
        включённых типов или mix.
    """
    # Импорт хелперов планнера здесь — избегаем циклической зависимости модулей.
    from app.pipeline.planner import _get_active_strategy, _get_enabled_types

    strategy_row = _get_active_strategy(db, project_id)
    if strategy_row is None:
        return []

    try:
        strategy_json = json.loads(strategy_row["strategy"]) if strategy_row["strategy"] else {}
    except (json.JSONDecodeError, TypeError):
        strategy_json = {}

    try:
        df = date.fromisoformat(date_from)
        dt = date.fromisoformat(date_to)
    except ValueError:
        logger.error("build_slots: неверный формат дат: %s — %s", date_from, date_to)
        return []
    if df > dt:
        return []

    # Включённые типы платформы
    enabled_map = _get_enabled_types(db, project_id, platform)
    enabled_types = {t for t, on in enabled_map.items() if on}
    if only_content_type is not None:
        if only_content_type not in enabled_types:
            return []
        enabled_types = {only_content_type}
    if not enabled_types:
        return []

    # Якорь фаз
    created_at = None
    try:
        created_at = strategy_row["created_at"]
    except (KeyError, IndexError):
        created_at = None
    anchor = _resolve_anchor(strategy_json, created_at)

    # Фазы (с легаси-слоем)
    raw_phases = strategy_json.get("phases")
    if isinstance(raw_phases, list) and raw_phases:
        phases = [p for p in raw_phases if isinstance(p, dict)]
    else:
        phases = []
    if not phases:
        phases = [_synth_legacy_phase(strategy_json)]

    best_times = _best_times(strategy_json, platform)

    # Активные директивы проекта (для override)
    directive_rows = db.execute(
        "SELECT id, parsed FROM directives "
        "WHERE project_id=? AND status='active' ORDER BY id",
        (project_id,),
    ).fetchall()
    directives: list[dict] = []
    for r in directive_rows:
        parsed = None
        if r["parsed"]:
            try:
                parsed = json.loads(r["parsed"])
            except (json.JSONDecodeError, TypeError):
                parsed = None
        directives.append({"id": r["id"], "parsed": parsed})

    # Существующие plan_items (дедуп) — один запрос по диапазону
    existing_rows = db.execute(
        "SELECT platform, content_type, date, time_slot FROM plan_items "
        "WHERE project_id=? AND platform=? AND status != 'rejected' "
        "AND date >= ? AND date <= ?",
        (project_id, platform, date_from, date_to),
    ).fetchall()
    # Существующие занятые ячейки — против них слот НЕ создаётся (дедуп, шаг 8).
    existing_occupied: set[tuple] = {
        (r["platform"], r["content_type"], r["date"], r["time_slot"])
        for r in existing_rows
    }
    # Занятые в ЭТОЙ генерации — против них слот сдвигается по времени (+30 мин).
    generated_occupied: set[tuple] = set()

    # Какие недели якоря пересекают [df; dt]
    k_from = week_index(anchor, df)
    k_to = week_index(anchor, dt)

    slots: list[dict] = []

    for k in range(k_from, k_to + 1):
        week_start = anchor + timedelta(days=7 * k)
        phase = _phase_for_week(phases, k)
        mix = _phase_mix_for_platform(phase, platform, enabled_types, only_content_type)
        mix = _apply_directives(mix, directives, platform, enabled_types, only_content_type)
        if not mix:
            continue

        goal_share = phase.get("goal_share", {})

        # Собираем слоты недели (по всем типам), затем присваиваем цели по неделе
        week_slots: list[dict] = []
        for ctype in sorted(mix.keys()):
            n = mix[ctype]
            if n <= 0:
                continue
            for i in range(n):
                day = week_start + timedelta(days=(i * 7) // n)
                day_iso = day.isoformat()
                time_slot = best_times[i % len(best_times)]
                # Коллизия с уже сгенерированным слотом → сдвиг +30 мин до свободного.
                key = (platform, ctype, day_iso, time_slot)
                guard = 0
                while key in generated_occupied and guard < 48:
                    time_slot = _bump_time(time_slot)
                    key = (platform, ctype, day_iso, time_slot)
                    guard += 1
                # Дедуп против существующих plan_items — такой слот не создаём.
                if key in existing_occupied:
                    continue
                generated_occupied.add(key)
                week_slots.append({
                    "date": day_iso,
                    "time_slot": time_slot,
                    "content_type": ctype,
                })

        if not week_slots:
            continue

        # Присвоить цели по goal_share недели, round-robin в порядке (date, time)
        week_slots.sort(key=lambda s: (s["date"], s["time_slot"]))
        goals = _assign_goals_round_robin(len(week_slots), goal_share)
        for s, g in zip(week_slots, goals):
            s["goal"] = g

        slots.extend(week_slots)

    # Отбросить слоты вне [df; dt]
    out: list[dict] = []
    for s in slots:
        try:
            sd = date.fromisoformat(s["date"])
        except ValueError:
            continue
        if sd < df or sd > dt:
            continue
        out.append(s)

    out.sort(key=lambda s: (s["date"], s["time_slot"]))
    return out
