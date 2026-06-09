"""
Фикстуры pytest.

- Временная БД на каждый тест (tmp_path).
- FAKE_LLM=1 — без сетевых вызовов.
- TestClient через httpx.
"""
import os
import pytest
from pathlib import Path
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def patch_env(tmp_path, monkeypatch):
    """
    Подменяет настройки окружения для изолированных тестов:
    - отдельная БД в tmp_path
    - FAKE_LLM=1 (без реальных вызовов LLM-Router)
    - реальный FERNET_KEY (генерируется одноразово)
    """
    from cryptography.fernet import Fernet
    db_file = str(tmp_path / "test.db")
    fernet_key = Fernet.generate_key().decode()

    monkeypatch.setenv("DB_PATH", db_file)
    monkeypatch.setenv("FAKE_LLM", "1")
    monkeypatch.setenv("FERNET_KEY", fernet_key)
    monkeypatch.setenv("LLM_ROUTER_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # Принудительно сбросить синглтон settings после подмены env
    import app.config as cfg_module
    cfg_module.settings = cfg_module.Settings()

    yield


@pytest.fixture
def client(patch_env):
    """
    TestClient с реальным приложением и временной БД.
    БД инициализируется при первом запросе через lifespan.
    """
    # Импортируем ПОСЛЕ patch_env, чтобы settings уже обновлены
    from app.db import init_db
    import app.config as cfg_module
    init_db(cfg_module.settings.db_path_absolute)

    from app.main import create_app
    application = create_app()

    with TestClient(application, raise_server_exceptions=True) as c:
        yield c
