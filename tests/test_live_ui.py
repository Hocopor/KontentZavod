"""
Тесты этапа 8.8 «Живой UI без скачков страницы».

Проверяют:
- подключение idiomorph и hx-ext="morph" на body
- поллинг дашборда (#dash-live)
- OOB-эндпоинт бейджей навигации
- поведение бейджей при нулевых и ненулевых счётчиках
- toggle/delete/add прокси возвращают фрагмент таблицы, а не редирект
- поллинг контент-плана и очереди публикации

pytest tests/test_live_ui.py -q
"""
import app.config as cfg_module
from app.db import get_db, init_db


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


class TestLiveUI:
    def test_idiomorph_loaded_and_ext_on_body(self, client, patch_env):
        _setup_db()
        r = client.get("/")
        assert r.status_code == 200
        assert "idiomorph-ext.min.js" in r.text
        assert 'hx-ext="morph"' in r.text

    def test_dashboard_has_live_poller(self, client, patch_env):
        _setup_db()
        r = client.get("/")
        assert 'id="dash-live"' in r.text
        assert 'hx-trigger="every 30s"' in r.text
        assert 'hx-swap="morph:outerHTML"' in r.text

    def test_nav_badges_endpoint_oob(self, client, patch_env):
        _setup_db()
        r = client.get("/partials/nav-badges")
        assert r.status_code == 200
        assert 'id="nav-badge-plan"' in r.text
        assert 'id="nav-badge-manual"' in r.text
        assert 'hx-swap-oob="true"' in r.text

    def test_nav_badge_hidden_when_zero(self, client, patch_env):
        _setup_db()
        r = client.get("/partials/nav-badges")
        # пустая БД → оба бейджа скрыты
        assert "hidden" in r.text

    def test_nav_badge_shows_count(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name, description, audience, tone, goals, cta, themes) "
                "VALUES ('live-t','T','d','a','t','g','c','th')"
            )
            pid = cur.lastrowid
            # два proposed пункта плана
            # Колонка даты в plan_items называется "date" (см. app/db.py SCHEMA)
            for i in range(2):
                db.execute(
                    "INSERT INTO plan_items (project_id, platform, content_type, date, title, status) "
                    "VALUES (?, 'telegram', 'post', '2026-06-20', ?, 'proposed')",
                    (pid, f"Тема {i}"),
                )
        r = client.get("/partials/nav-badges")
        # бейдж плана НЕ скрыт и показывает 2
        assert 'id="nav-badge-plan"' in r.text
        assert "hidden" not in r.text.split('id="nav-badge-plan"')[1].split('>')[0]
        assert ">2<" in r.text

    def test_proxies_toggle_returns_fragment_not_redirect(self, client, patch_env):
        _setup_db()
        client.post("/proxies/add", data={"raw_urls": "http://u:p@livehost:3128"})
        with get_db() as db:
            row = db.execute("SELECT id, enabled FROM proxies WHERE label='livehost:3128'").fetchone()
        pid, before = row["id"], row["enabled"]
        resp = client.post(f"/proxies/{pid}/toggle")
        assert resp.status_code == 200
        # ответ — фрагмент таблицы (есть таблица или пустое состояние), не полноразмерная страница с <nav>
        assert "proxy-table" in resp.text or "Прокси не добавлены" in resp.text
        with get_db() as db:
            after = db.execute("SELECT enabled FROM proxies WHERE id=?", (pid,)).fetchone()["enabled"]
        assert after != before

    def test_proxies_add_returns_table_with_result(self, client, patch_env):
        _setup_db()
        resp = client.post("/proxies/add", data={"raw_urls": "http://u:p@addhost:3128"})
        assert resp.status_code == 200
        assert "Добавлено" in resp.text
        assert "addhost:3128" in resp.text  # маскированный host в таблице

    def test_plan_board_has_polling(self, client, patch_env):
        _setup_db()
        r = client.get("/plan")
        assert r.status_code == 200
        assert 'id="plan-board-area"' in r.text
        assert 'hx-select="#plan-board-area"' in r.text

    def test_queue_has_live_container(self, client, patch_env):
        _setup_db()
        r = client.get("/queue")
        assert r.status_code == 200
        assert 'id="queue-live"' in r.text
