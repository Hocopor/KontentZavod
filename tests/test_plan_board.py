"""
Тесты шахматки согласования контент-плана (этап 7, волна C).

Покрывает:
- GET /plan: страница 200, с проектом и пунктами (платформы/типы/title видны)
- Переключение месяца: пункты другого месяца не видны
- GET /plan/item/{id}: 200 с brief-полями
- POST /plan/item/{id}/save: меняет title, дату, brief
- POST /plan/item/{id}/approve: меняет статус proposed → approved
- POST /plan/item/{id}/reject: меняет статус → rejected
- POST /plan/approve-period: одобряет только proposed текущего месяца
- POST /plan/autogen/{slug}: toggle autogen в settings
- Страница без проектов: 200, пустое состояние
- GET /plan/item/99999: 404 для несуществующего item
"""
import json
import uuid
from datetime import date

import pytest

import app.config as cfg_module
from app.db import get_db, init_db, get_project_settings


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"plan-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, stage="running", platforms=("telegram", "vk")):
    """Создать проект с указанными платформами. Вернуть (slug, project_id)."""
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Шахматка', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?)
        """,
        (s, stage),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, enabled, "auto"),
        )
    return s, project_id


def _create_plan_item(
    db,
    project_id: int,
    *,
    platform="telegram",
    content_type="post",
    item_date="2026-06-15",
    time_slot="10:00",
    title="Тестовый пункт плана",
    status="proposed",
    brief: dict | None = None,
    content_id: int | None = None,
):
    """Создать plan_item и вернуть его id."""
    if brief is None:
        brief = {
            "hook": "Тестовый хук",
            "outline": "Тезис 1\nТезис 2",
            "cta": "Подпишись",
            "keywords": ["контент", "тест"],
            "rubric": "Советы",
        }
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, brief, status, content_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            platform,
            content_type,
            item_date,
            time_slot,
            title,
            json.dumps(brief, ensure_ascii=False),
            status,
            content_id,
        ),
    )
    return cur.lastrowid


# ─── 1. Страница шахматки ─────────────────────────────────────────────────────


class TestPlanBoard:

    def test_board_200_with_project_and_items(self, client, patch_env):
        """Страница /plan возвращает 200 и показывает платформы, тип, title."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", title="Статья о котах")

        resp = client.get(f"/plan?project={s}&year=2026&month=6")
        assert resp.status_code == 200
        body = resp.text
        assert "Статья о котах" in body
        assert "Telegram" in body
        assert "Пост" in body  # label из каталога

    def test_board_200_no_projects(self, client, patch_env):
        """Страница без проектов: 200, пустое состояние."""
        _setup_db()
        resp = client.get("/plan")
        assert resp.status_code == 200
        assert "Нет активных проектов" in resp.text

    def test_board_default_month_current(self, client, patch_env):
        """Без year/month открывается текущий месяц."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))

        resp = client.get(f"/plan?project={s}")
        assert resp.status_code == 200
        today = date.today()
        # Название месяца должно быть на странице
        month_names = [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
        ]
        assert month_names[today.month - 1] in resp.text

    def test_board_month_switch_items_not_visible(self, client, patch_env):
        """Пункты другого месяца не отображаются в шахматке текущего месяца."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            _create_plan_item(db, pid, item_date="2026-06-15", title="Июньский пост")
            _create_plan_item(db, pid, item_date="2026-07-10", title="Июльский пост")

        # Смотрим июнь — июльского поста нет
        resp = client.get(f"/plan?project={s}&year=2026&month=6")
        assert resp.status_code == 200
        assert "Июньский пост" in resp.text
        assert "Июльский пост" not in resp.text

        # Смотрим июль — июньского поста нет
        resp2 = client.get(f"/plan?project={s}&year=2026&month=7")
        assert resp2.status_code == 200
        assert "Июльский пост" in resp2.text
        assert "Июньский пост" not in resp2.text

    def test_board_selects_running_project_by_default(self, client, patch_env):
        """По умолчанию выбирается первый running-проект."""
        _setup_db()
        with get_db() as db:
            s_draft, pid1 = _create_project(db, stage="draft", platforms=("telegram",))
            s_run, pid2 = _create_project(db, stage="running", platforms=("telegram",))
            _create_plan_item(db, pid2, item_date="2026-06-15", title="Running-проект пост")

        resp = client.get("/plan?year=2026&month=6")
        assert resp.status_code == 200
        assert "Running-проект пост" in resp.text

    def test_board_today_highlighted(self, client, patch_env):
        """Сегодняшний день подсвечивается (атрибут today-col или класс)."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
        today = date.today()
        resp = client.get(f"/plan?project={s}&year={today.year}&month={today.month}")
        assert resp.status_code == 200
        # В шаблоне today-col присваивается ячейкам сегодняшнего дня
        assert "today-col" in resp.text or "accent" in resp.text

    def test_board_multi_platform(self, client, patch_env):
        """Несколько платформ отображаются отдельными строками."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram", "vk"))
            _create_plan_item(db, pid, platform="telegram", title="TG-пост")
            _create_plan_item(db, pid, platform="vk", title="VK-пост", content_type="post")

        resp = client.get(f"/plan?project={s}&year=2026&month=6")
        assert resp.status_code == 200
        assert "TG-пост" in resp.text
        assert "VK-пост" in resp.text
        assert "Telegram" in resp.text
        assert "ВКонтакте" in resp.text


# ─── 2. Модалка пункта плана ──────────────────────────────────────────────────


class TestPlanItemModal:

    def test_modal_200_with_brief_fields(self, client, patch_env):
        """GET /plan/item/{id}: 200 и все brief-поля видны."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(
                db, pid,
                title="Кот в интернете",
                brief={
                    "hook": "Все любят котов",
                    "outline": "Факты о котах",
                    "cta": "Поделись с друзьями",
                    "keywords": ["коты", "интернет"],
                    "rubric": "Развлечения",
                },
            )

        resp = client.get(f"/plan/item/{iid}")
        assert resp.status_code == 200
        body = resp.text
        assert "Кот в интернете" in body
        assert "Все любят котов" in body
        assert "Факты о котах" in body
        assert "Поделись с друзьями" in body
        assert "коты" in body
        assert "Развлечения" in body

    def test_modal_404_nonexistent(self, client, patch_env):
        """GET /plan/item/99999: 404 для несуществующего item."""
        _setup_db()
        resp = client.get("/plan/item/99999")
        assert resp.status_code == 404

    def test_modal_shows_status_label(self, client, patch_env):
        """Модалка показывает человекочитаемый статус."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="approved")

        resp = client.get(f"/plan/item/{iid}")
        assert resp.status_code == 200
        assert "Одобрен" in resp.text

    def test_modal_shows_error_text(self, client, patch_env):
        """Модалка показывает error_text при статусе error."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="error")
            db.execute(
                "UPDATE plan_items SET error_text='LLM timeout' WHERE id=?", (iid,)
            )

        resp = client.get(f"/plan/item/{iid}")
        assert resp.status_code == 200
        assert "LLM timeout" in resp.text

    def test_modal_generated_shows_content_link(self, client, patch_env):
        """Модалка с status=generated и content_id показывает ссылку на контент."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            # Создать content-запись
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
                (pid, "post", "Готовый пост", "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.get(f"/plan/item/{iid}")
        assert resp.status_code == 200
        assert "/review/" in resp.text or "контент сгенерирован" in resp.text.lower() or "Перейти к контенту" in resp.text


# ─── 3. Сохранение правок ─────────────────────────────────────────────────────


class TestPlanItemSave:

    def test_save_updates_title(self, client, patch_env):
        """POST save меняет title в БД."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, title="Старый заголовок")

        resp = client.post(f"/plan/item/{iid}/save", data={
            "title": "Новый заголовок",
            "date": "2026-06-15",
            "time_slot": "12:00",
            "hook": "", "outline": "", "cta": "", "keywords": "", "rubric": "",
        })
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT title FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["title"] == "Новый заголовок"

    def test_save_updates_date(self, client, patch_env):
        """POST save меняет дату в БД."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, item_date="2026-06-10")

        resp = client.post(f"/plan/item/{iid}/save", data={
            "title": "Пост",
            "date": "2026-06-20",
            "time_slot": "",
            "hook": "", "outline": "", "cta": "", "keywords": "", "rubric": "",
        })
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT date FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["date"] == "2026-06-20"

    def test_save_updates_brief_fields(self, client, patch_env):
        """POST save меняет поля brief (hook, outline, cta, keywords, rubric)."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid)

        resp = client.post(f"/plan/item/{iid}/save", data={
            "title": "Пост",
            "date": "2026-06-15",
            "time_slot": "09:00",
            "hook": "Новый хук",
            "outline": "Новые тезисы",
            "cta": "Новый CTA",
            "keywords": "ключ1, ключ2",
            "rubric": "Новая рубрика",
        })
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT brief FROM plan_items WHERE id=?", (iid,)).fetchone()
        brief = json.loads(row["brief"])
        assert brief["hook"] == "Новый хук"
        assert brief["outline"] == "Новые тезисы"
        assert brief["cta"] == "Новый CTA"
        assert "ключ1" in brief["keywords"]
        assert "ключ2" in brief["keywords"]
        assert brief["rubric"] == "Новая рубрика"

    def test_save_invalid_date_returns_error(self, client, patch_env):
        """POST save с неверной датой возвращает ошибку валидации."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid)

        resp = client.post(f"/plan/item/{iid}/save", data={
            "title": "Пост",
            "date": "не-дата",
            "time_slot": "",
            "hook": "", "outline": "", "cta": "", "keywords": "", "rubric": "",
        })
        assert resp.status_code == 200
        assert "Неверный формат даты" in resp.text

    def test_save_invalid_time_returns_error(self, client, patch_env):
        """POST save с неверным временем возвращает ошибку."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid)

        resp = client.post(f"/plan/item/{iid}/save", data={
            "title": "Пост",
            "date": "2026-06-15",
            "time_slot": "25:99",
            "hook": "", "outline": "", "cta": "", "keywords": "", "rubric": "",
        })
        assert resp.status_code == 200
        assert "Время вне диапазона" in resp.text or "Неверный формат" in resp.text

    def test_save_404_nonexistent(self, client, patch_env):
        """POST save на несуществующий id → 404."""
        _setup_db()
        resp = client.post("/plan/item/99999/save", data={
            "title": "X", "date": "2026-06-15", "time_slot": "",
            "hook": "", "outline": "", "cta": "", "keywords": "", "rubric": "",
        })
        assert resp.status_code == 404


# ─── 4. Одобрение / Отклонение ────────────────────────────────────────────────


class TestPlanItemApproveReject:

    def test_approve_proposed_to_approved(self, client, patch_env):
        """POST approve: proposed → approved."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="proposed")

        resp = client.post(f"/plan/item/{iid}/approve")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "approved"

    def test_approve_rejected_to_approved(self, client, patch_env):
        """POST approve: rejected → approved."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="rejected")

        resp = client.post(f"/plan/item/{iid}/approve")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "approved"

    def test_approve_error_to_approved(self, client, patch_env):
        """POST approve: error → approved."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="error")

        resp = client.post(f"/plan/item/{iid}/approve")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "approved"

    def test_approve_generated_stays_generated(self, client, patch_env):
        """POST approve на generated: статус НЕ меняется (нельзя одобрить уже готовое)."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="generated")

        resp = client.post(f"/plan/item/{iid}/approve")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "generated"

    def test_reject_proposed_to_rejected(self, client, patch_env):
        """POST reject: proposed → rejected."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="proposed")

        resp = client.post(f"/plan/item/{iid}/reject")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "rejected"

    def test_reject_approved_to_rejected(self, client, patch_env):
        """POST reject: approved → rejected."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="approved")

        resp = client.post(f"/plan/item/{iid}/reject")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "rejected"

    def test_approve_404_nonexistent(self, client, patch_env):
        """POST approve на несуществующий id → 404."""
        _setup_db()
        resp = client.post("/plan/item/99999/approve")
        assert resp.status_code == 404

    def test_reject_404_nonexistent(self, client, patch_env):
        """POST reject на несуществующий id → 404."""
        _setup_db()
        resp = client.post("/plan/item/99999/reject")
        assert resp.status_code == 404

    def test_modal_shows_approve_button_for_proposed(self, client, patch_env):
        """Модалка proposed-пункта содержит кнопку «Одобрить»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="proposed")

        resp = client.get(f"/plan/item/{iid}")
        assert "Одобрить" in resp.text

    def test_modal_shows_reject_button(self, client, patch_env):
        """Модалка содержит кнопку «Отклонить» для не-rejected статуса."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="proposed")

        resp = client.get(f"/plan/item/{iid}")
        assert "Отклонить" in resp.text


# ─── 5. Одобрение периода ─────────────────────────────────────────────────────


class TestApprovePeriod:

    def test_approve_period_only_proposed_this_month(self, client, patch_env):
        """
        POST /plan/approve-period одобряет только proposed текущего месяца;
        rejected и другие месяцы не трогаются.
        """
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            # proposed июня
            i1 = _create_plan_item(db, pid, item_date="2026-06-10", status="proposed", title="Предложенный")
            # rejected июня — не должен стать approved
            i2 = _create_plan_item(db, pid, item_date="2026-06-20", status="rejected", title="Отклонённый")
            # proposed июля — не должен стать approved
            i3 = _create_plan_item(db, pid, item_date="2026-07-05", status="proposed", title="Июльский")
            # approved июня — остаётся approved
            i4 = _create_plan_item(db, pid, item_date="2026-06-25", status="approved", title="Уже одобрен")

        resp = client.post("/plan/approve-period", data={
            "project_id": str(pid),
            "year": "2026",
            "month": "6",
        })
        # Ожидаем редирект на /plan
        assert resp.status_code in (200, 303, 302)

        with get_db() as db:
            r1 = db.execute("SELECT status FROM plan_items WHERE id=?", (i1,)).fetchone()
            r2 = db.execute("SELECT status FROM plan_items WHERE id=?", (i2,)).fetchone()
            r3 = db.execute("SELECT status FROM plan_items WHERE id=?", (i3,)).fetchone()
            r4 = db.execute("SELECT status FROM plan_items WHERE id=?", (i4,)).fetchone()

        assert r1["status"] == "approved"    # proposed июня → approved
        assert r2["status"] == "rejected"    # rejected не трогается
        assert r3["status"] == "proposed"    # июль не трогается
        assert r4["status"] == "approved"    # уже approved — не меняется

    def test_approve_period_no_items_no_error(self, client, patch_env):
        """POST approve-period без пунктов в месяце не вызывает ошибку."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)

        resp = client.post("/plan/approve-period", data={
            "project_id": str(pid),
            "year": "2026",
            "month": "6",
        })
        assert resp.status_code in (200, 303, 302)


# ─── 6. Toggle autogen ────────────────────────────────────────────────────────


class TestAutogenToggle:

    def test_autogen_toggles_on(self, client, patch_env):
        """POST /plan/autogen/{slug} включает autogen=1 если был 0."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)

        resp = client.post(f"/plan/autogen/{s}", follow_redirects=False)
        assert resp.status_code in (200, 303, 302)

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (s,)).fetchone()
        settings = get_project_settings(row["settings"])
        assert settings["autogen"] == 1

    def test_autogen_toggles_off(self, client, patch_env):
        """POST /plan/autogen/{slug} выключает autogen=0 если был 1."""
        _setup_db()
        import json as _json
        with get_db() as db:
            s, pid = _create_project(db)
            # Установить autogen=1 вручную
            db.execute(
                "UPDATE projects SET settings=? WHERE slug=?",
                (_json.dumps({"autogen": 1}), s),
            )

        resp = client.post(f"/plan/autogen/{s}", follow_redirects=False)
        assert resp.status_code in (200, 303, 302)

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (s,)).fetchone()
        settings = get_project_settings(row["settings"])
        assert settings["autogen"] == 0

    def test_autogen_toggle_twice_returns_to_original(self, client, patch_env):
        """Двойной toggle возвращает к исходному значению autogen=0."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)

        client.post(f"/plan/autogen/{s}", follow_redirects=False)
        client.post(f"/plan/autogen/{s}", follow_redirects=False)

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (s,)).fetchone()
        settings = get_project_settings(row["settings"])
        assert settings["autogen"] == 0

    def test_autogen_toggle_404_unknown_slug(self, client, patch_env):
        """POST autogen на несуществующий slug → 404."""
        _setup_db()
        resp = client.post("/plan/autogen/nonexistent-slug", follow_redirects=False)
        assert resp.status_code == 404

    def test_autogen_shown_on_board(self, client, patch_env):
        """Состояние autogen отображается на странице шахматки (кнопка «Генерация: ВКЛ/ВЫКЛ»)."""
        _setup_db()
        import json as _json
        with get_db() as db:
            s, pid = _create_project(db)
            db.execute(
                "UPDATE projects SET settings=? WHERE slug=?",
                (_json.dumps({"autogen": 1}), s),
            )

        resp = client.get(f"/plan?project={s}&year=2026&month=6")
        assert resp.status_code == 200
        assert "ВКЛ" in resp.text


# ─── 7. Навигация ─────────────────────────────────────────────────────────────


class TestPlanNavigation:

    def test_plan_link_in_nav(self, client, patch_env):
        """Ссылка «🗓 План» присутствует в навигации всех страниц."""
        _setup_db()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "/plan" in resp.text

    def test_plan_month_navigation_links(self, client, patch_env):
        """На странице плана есть ссылки на предыдущий/следующий месяц."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)

        resp = client.get(f"/plan?project={s}&year=2026&month=6")
        assert resp.status_code == 200
        # Ссылки на май и июль
        assert "month=5" in resp.text or "month=7" in resp.text

    def test_plan_project_selector_multiple(self, client, patch_env):
        """Если проектов несколько — на странице есть селектор."""
        _setup_db()
        with get_db() as db:
            s1, _ = _create_project(db, slug=f"p1-{uuid.uuid4().hex[:4]}")
            s2, _ = _create_project(db, slug=f"p2-{uuid.uuid4().hex[:4]}")

        resp = client.get(f"/plan?project={s1}&year=2026&month=6")
        assert resp.status_code == 200
        # Селектор или имена обоих проектов
        assert s1 in resp.text or "select" in resp.text.lower()
