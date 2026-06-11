"""
Тесты подсистемы HTTP-прокси.

Покрытие:
    1. parse_proxy_input — curl-команда, голый URL, socks5, несколько строк, мусор.
    2. Фейловер request_via_proxy — первый прокси падает → запрос идёт через второй.
    3. Нет прокси → прямой запрос.
    4. Веб-эндпоинты: add, toggle, delete, маскировка пароля в GET /proxies.

pytest tests/test_proxies.py -q
"""
import json
import sqlite3

import httpx
import pytest

from app.services.proxies import parse_proxy_input, mask_proxy_url


# ─── 1. parse_proxy_input ─────────────────────────────────────────────────────

class TestParseProxyInput:
    def test_curl_command_quoted(self):
        raw = 'curl --proxy "http://user:pass@host.example.com:8080/" https://ipv4.webshare.io/'
        urls = parse_proxy_input(raw)
        assert len(urls) == 1
        assert "host.example.com:8080" in urls[0]
        assert urls[0].startswith("http://")

    def test_bare_url(self):
        raw = "http://user:secret@192.168.1.1:3128"
        urls = parse_proxy_input(raw)
        assert len(urls) == 1
        assert urls[0].startswith("http://user:secret@192.168.1.1:3128")

    def test_socks5_parsed(self):
        raw = "socks5://proxyuser:proxypass@proxy.example.com:1080"
        urls = parse_proxy_input(raw)
        assert len(urls) == 1
        assert urls[0].startswith("socks5://")
        assert "proxy.example.com" in urls[0]

    def test_multiple_lines(self):
        raw = (
            "http://user1:p1@proxy1.example.com:3128\n"
            "http://user2:p2@proxy2.example.com:3128\n"
            "socks5://user3:p3@proxy3.example.com:1080\n"
        )
        urls = parse_proxy_input(raw)
        assert len(urls) == 3

    def test_garbage_lines_ignored(self):
        raw = (
            "это мусор\n"
            "не URL вообще\n"
            "http://valid.proxy:3128\n"
            "12345\n"
        )
        urls = parse_proxy_input(raw)
        assert len(urls) == 1
        assert "valid.proxy" in urls[0]

    def test_trailing_slash_normalized(self):
        raw = "http://host:3128"
        urls = parse_proxy_input(raw)
        assert urls[0].endswith("/")

    def test_empty_input(self):
        assert parse_proxy_input("") == []
        assert parse_proxy_input("   \n  ") == []

    def test_dedup_same_host_port(self):
        raw = (
            "http://user:pass1@host:3128\n"
            'curl --proxy "http://user:pass2@host:3128/" https://example.com\n'
        )
        urls = parse_proxy_input(raw)
        # Одна строка → один URL, второй — дубликат
        assert len(urls) == 1


# ─── 2. Маскировка URL ────────────────────────────────────────────────────────

class TestMaskProxyUrl:
    def test_masks_password(self):
        url = "http://user:mysecretpassword@host:3128/"
        masked = mask_proxy_url(url)
        assert "mysecretpassword" not in masked
        assert "user:***" in masked
        assert "host:3128" in masked

    def test_no_credentials(self):
        url = "http://host:3128/"
        masked = mask_proxy_url(url)
        assert masked == url


# ─── 3. Фейловер request_via_proxy ───────────────────────────────────────────

class FakeResponse:
    """Минимальная заглушка httpx.Response."""
    status_code = 200
    text = "ok"


class TestFailover:
    """
    Тест фейловера: два прокси в БД.
    Первый прокси кидает ConnectError, второй отвечает OK.
    Ожидаем: запрос ушёл через второй, у первого fail_count=1.
    """

    def _setup_db(self, tmp_path):
        """Создаёт тестовую БД с двумя прокси."""
        from app.db import init_db, get_db
        import app.config as cfg_module
        import app.db as db_module

        db_path = str(tmp_path / "test.db")
        db_module.settings = cfg_module.settings  # обновляем на патченные настройки

        init_db(db_path)

        from app.security import encrypt
        enc1 = encrypt("http://proxy1.example.com:3128/")
        enc2 = encrypt("http://proxy2.example.com:3128/")

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO proxies (label, url_encrypted, enabled, priority) VALUES (?, ?, 1, 1)",
            ("proxy1", enc1),
        )
        conn.execute(
            "INSERT INTO proxies (label, url_encrypted, enabled, priority) VALUES (?, ?, 1, 2)",
            ("proxy2", enc2),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_failover_first_fails(self, tmp_path, monkeypatch):
        """Первый прокси падает → запрос идёт через второй, у первого fail_count=1."""
        from app.db import get_db, init_db
        import app.config as cfg_module

        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("FERNET_KEY", __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode())

        db_path = self._setup_db(tmp_path)
        monkeypatch.setenv("DB_PATH", db_path)

        # Пересоздаём settings после monkeypatch
        import importlib
        import app.config
        importlib.reload(app.config)
        from app.config import settings as new_settings
        import app.db as db_mod
        db_mod.settings = new_settings

        call_order = []

        class FakeClient:
            def __init__(self, proxy=None, timeout=None):
                self._proxy = proxy

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def request(self, method, url, **kwargs):
                call_order.append(self._proxy)
                if "proxy1" in (self._proxy or ""):
                    raise httpx.ConnectError("Не удалось подключиться к proxy1")
                return FakeResponse()

        monkeypatch.setattr("app.services.proxies.httpx.Client", FakeClient)

        from app.services.proxies import request_via_proxy

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        resp = request_via_proxy("GET", "https://api.telegram.org/", _db=conn)
        assert resp.status_code == 200

        # Первый прокси должен иметь fail_count=1
        rows = conn.execute("SELECT label, fail_count, last_ok_at FROM proxies ORDER BY priority").fetchall()
        assert rows[0]["fail_count"] == 1, f"Ожидался fail_count=1 у proxy1, получили {rows[0]['fail_count']}"
        # Второй прокси — last_ok_at заполнен
        assert rows[1]["last_ok_at"] is not None, "last_ok_at у proxy2 должен быть заполнен"
        assert rows[1]["fail_count"] == 0

        # Убеждаемся, что реально был вызов через proxy2
        assert any("proxy2" in (p or "") for p in call_order)

        conn.close()

    def test_no_proxies_direct_request(self, tmp_path, monkeypatch):
        """Если прокси не настроены → прямой запрос."""
        from cryptography.fernet import Fernet
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())

        import app.config
        import importlib
        importlib.reload(app.config)
        import app.db as db_mod
        db_mod.settings = app.config.settings

        from app.db import init_db
        init_db(str(tmp_path / "test.db"))

        direct_calls = []

        class FakeDirectClient:
            def __init__(self, proxy=None, timeout=None):
                self._proxy = proxy

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def request(self, method, url, **kwargs):
                direct_calls.append(self._proxy)
                return FakeResponse()

        monkeypatch.setattr("app.services.proxies.httpx.Client", FakeDirectClient)

        from app.services.proxies import request_via_proxy
        resp = request_via_proxy("GET", "https://api.telegram.org/")
        assert resp.status_code == 200
        # Прямой запрос — proxy=None
        assert direct_calls and direct_calls[0] is None


# ─── 4. Веб-эндпоинты ────────────────────────────────────────────────────────

class TestProxiesWeb:
    """
    Тесты HTTP-интерфейса управления прокси.
    """

    def test_add_curl_format(self, client):
        """Добавление через curl-команду → запись в БД, URL зашифрован, пароль не виден в БД."""
        raw = 'curl --proxy "http://webuser:s3cr3t@proxy.webshare.io:80/" https://ipv4.webshare.io/'
        resp = client.post("/proxies/add", data={"raw_urls": raw}, follow_redirects=True)
        assert resp.status_code == 200

        # Прокси появился на странице
        html = resp.text
        # URL замаскирован — пароль не должен встречаться в HTML
        assert "s3cr3t" not in html
        # host:port должен быть виден
        assert "proxy.webshare.io" in html

    def test_password_not_in_db_raw(self, client):
        """Пароль не должен храниться в открытом виде в БД."""
        raw = "http://dbuser:dbpassword123@host:3128"
        client.post("/proxies/add", data={"raw_urls": raw}, follow_redirects=True)

        # Читаем сырое значение из БД
        from app.db import get_db
        with get_db() as db:
            row = db.execute("SELECT url_encrypted FROM proxies WHERE label LIKE '%host%'").fetchone()

        assert row is not None
        raw_stored = row["url_encrypted"]
        assert "dbpassword123" not in raw_stored, "Пароль не должен быть в открытом виде в БД"

    def test_toggle(self, client):
        """toggle переключает enabled."""
        client.post("/proxies/add", data={"raw_urls": "http://user:pw@toggle-host:3128"}, follow_redirects=True)

        from app.db import get_db
        with get_db() as db:
            row = db.execute("SELECT id, enabled FROM proxies WHERE url_encrypted LIKE '%'").fetchone()

        assert row is not None
        proxy_id = row["id"]
        initial_enabled = row["enabled"]

        client.post(f"/proxies/{proxy_id}/toggle", follow_redirects=True)

        with get_db() as db:
            row2 = db.execute("SELECT enabled FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        assert row2["enabled"] == (1 - initial_enabled)

    def test_delete(self, client):
        """delete удаляет прокси из БД."""
        client.post("/proxies/add", data={"raw_urls": "http://user:pw@delete-host:3128"}, follow_redirects=True)

        from app.db import get_db
        with get_db() as db:
            row = db.execute("SELECT id FROM proxies WHERE label = 'delete-host:3128'").fetchone()

        assert row is not None
        proxy_id = row["id"]

        client.post(f"/proxies/{proxy_id}/delete", follow_redirects=True)

        with get_db() as db:
            row2 = db.execute("SELECT id FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        assert row2 is None

    def test_masked_url_in_page(self, client):
        """Пароль не должен встречаться в HTML GET /proxies."""
        raw = "http://visibleuser:hiddenpassword@masked-proxy:3128/"
        client.post("/proxies/add", data={"raw_urls": raw}, follow_redirects=True)

        resp = client.get("/proxies")
        assert resp.status_code == 200
        html = resp.text
        assert "hiddenpassword" not in html, "Пароль не должен отображаться в интерфейсе"
        assert "visibleuser" in html, "Имя пользователя должно быть видно"
        assert "masked-proxy" in html, "Хост должен быть виден"

    def test_no_duplicates(self, client):
        """Дубликат по host:port не добавляется повторно."""
        raw = "http://user:pass@dedup-host:3128"
        client.post("/proxies/add", data={"raw_urls": raw}, follow_redirects=True)
        client.post("/proxies/add", data={"raw_urls": raw}, follow_redirects=True)

        from app.db import get_db
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM proxies WHERE label = 'dedup-host:3128'"
            ).fetchone()[0]
        assert count == 1

    def test_get_proxies_page_ok(self, client):
        """GET /proxies → 200."""
        resp = client.get("/proxies")
        assert resp.status_code == 200
        assert "Прокси" in resp.text

    def test_add_invalid_input(self, client):
        """Добавление полностью невалидного ввода → страница с ошибкой."""
        resp = client.post("/proxies/add", data={"raw_urls": "это мусор\n12345\nнет url"}, follow_redirects=True)
        assert resp.status_code == 200
        assert "валидных" in resp.text.lower() or "не найдено" in resp.text.lower()

    def test_check_endpoint_socks5(self, client):
        """check_proxy для socks5 возвращает ошибку (нет пакета httpx[socks])."""
        from app.services.proxies import check_proxy
        ok, msg = check_proxy("socks5://user:pass@host:1080/")
        assert ok is False
        assert "socks5" in msg.lower() or "http" in msg.lower()
