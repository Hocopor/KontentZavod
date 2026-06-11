"""
Тесты CRUD проектов.

Покрывает:
1. Создание проекта — slug генерируется корректно
2. Список проектов — новый проект виден
3. Редактирование профиля
4. Настройка площадки с токеном:
   - в БД хранится НЕ плейнтекст
   - decrypt возвращает исходный токен
5. Удаление пустого проекта — ОК
6. Каскадное удаление проекта с контентом + schedule + файлами
7. Архивация и разархивация
"""
import json
import sqlite3
from pathlib import Path

import pytest


# ─── Вспомогательная функция ─────────────────────────────────────────────────

def create_project(client, name="Тест Проект", **kwargs):
    data = {"name": name, **kwargs}
    resp = client.post("/projects/new", data=data, follow_redirects=False)
    assert resp.status_code == 303, f"Ожидали 303, получили {resp.status_code}"
    slug = resp.headers["location"].rstrip("/").split("/")[-1]
    return slug


# ─── Тесты ───────────────────────────────────────────────────────────────────

def test_create_project_slug_generated(client):
    """Создание проекта — slug транслитерируется из имени."""
    slug = create_project(client, name="Фитнес Клуб")
    assert slug != ""
    # Slug должен быть латиницей и дефисами
    assert slug.replace("-", "").isalnum(), f"Slug содержит недопустимые символы: {slug}"
    # Убедиться, что проект есть в БД
    resp = client.get(f"/projects/{slug}")
    assert resp.status_code == 200
    assert "Фитнес Клуб" in resp.text


def test_create_project_slug_ascii_name(client):
    """Slug из ASCII-имени — без транслитерации."""
    slug = create_project(client, name="My Project")
    assert "my" in slug and "project" in slug


def test_create_project_slug_uniqueness(client):
    """Дублирующиеся имена получают суффикс -2, -3."""
    slug1 = create_project(client, name="Тестовый")
    slug2 = create_project(client, name="Тестовый")
    assert slug1 != slug2
    assert slug2.endswith("-2")


def test_projects_list(client):
    """Созданный проект появляется в списке."""
    create_project(client, name="Виден в Списке")
    resp = client.get("/projects")
    assert resp.status_code == 200
    assert "Виден в Списке" in resp.text


def test_edit_project(client):
    """Редактирование профиля — данные обновляются."""
    slug = create_project(client, name="До правки")
    resp = client.post(
        f"/projects/{slug}/edit",
        data={
            "name": "После правки",
            "description": "Новое описание",
            "audience": "Все люди мира",
            "tone": "Дружелюбный",
            "goals": "Продажи",
            "cta": "Купить",
            "links": "",
            "themes": "",
            "forbidden": "",
            "extra": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get(f"/projects/{slug}")
    assert "После правки" in resp.text
    assert "Новое описание" in resp.text
    assert "Все люди мира" in resp.text


def test_platform_token_encrypted(client, patch_env):
    """Токен площадки шифруется: в БД не плейнтекст, decrypt отдаёт исходник."""
    import app.config as cfg_module
    slug = create_project(client, name="Шифрование токена")

    plain_token = "my-secret-tg-bot-token-123"
    resp = client.post(
        f"/projects/{slug}/platform",
        data={
            "platform": "telegram",
            "enabled": "1",
            "mode": "auto",
            "bot_token": plain_token,
            "chat_id": "@testchannel",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Читаем напрямую из БД
    db_path = str(cfg_module.settings.db_path_absolute)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT pp.credentials
        FROM project_platforms pp
        JOIN projects p ON p.id = pp.project_id
        WHERE p.slug = ? AND pp.platform = 'telegram'
        """,
        (slug,),
    ).fetchone()
    conn.close()

    assert row is not None
    stored = row["credentials"]
    # В БД должен быть НЕ плейнтекст
    assert stored != plain_token, "Токен хранится в открытом виде — это ошибка!"
    assert len(stored) > 20, "credentials выглядит подозрительно коротко"

    # Decrypt возвращает исходник — новый формат использует bot_token
    from app.security import decrypt
    decrypted_json = decrypt(stored)
    data = json.loads(decrypted_json)
    assert data["bot_token"] == plain_token


def test_platform_token_not_changed_on_empty(client, patch_env):
    """Пустой ввод токена — старый credentials остаётся без изменений."""
    import app.config as cfg_module
    slug = create_project(client, name="Сохранение токена")

    # Первый раз — сохраняем токен через named field (vk: access_token)
    client.post(
        f"/projects/{slug}/platform",
        data={"platform": "vk", "enabled": "1", "mode": "auto",
              "group_id": "123", "access_token": "initial-vk-token"},
        follow_redirects=False,
    )

    # Второй раз — пустой токен
    client.post(
        f"/projects/{slug}/platform",
        data={"platform": "vk", "enabled": "0", "mode": "manual",
              "group_id": "456"},
        follow_redirects=False,
    )

    db_path = str(cfg_module.settings.db_path_absolute)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT pp.credentials FROM project_platforms pp JOIN projects p ON p.id=pp.project_id WHERE p.slug=? AND pp.platform='vk'",
        (slug,),
    ).fetchone()
    conn.close()

    from app.security import decrypt
    data = json.loads(decrypt(row["credentials"]))
    assert data["access_token"] == "initial-vk-token"


def test_delete_empty_project(client):
    """Удаление проекта без контента — OK, редирект на /projects."""
    slug = create_project(client, name="Удалить меня")
    resp = client.post(f"/projects/{slug}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"

    # Проверить что проекта нет
    resp = client.get(f"/projects/{slug}")
    assert resp.status_code == 404


def test_delete_project_with_content_cascades(client, patch_env):
    """
    Каскадное удаление: проект с контентом + schedule + временными файлами →
    303 на /projects, проект исчезает, контент и schedule тоже удалены, файлы удалены.
    """
    import app.config as cfg_module
    slug = create_project(client, name="Каскадное удаление")

    db_path = str(cfg_module.settings.db_path_absolute)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    project_id = conn.execute(
        "SELECT id FROM projects WHERE slug=?", (slug,)
    ).fetchone()["id"]

    # Создаём контент
    cur = conn.execute(
        "INSERT INTO content (project_id, type, title, status) VALUES (?, 'post', 'Пост', 'approved')",
        (project_id,),
    )
    content_id = cur.lastrowid

    # Создаём schedule
    conn.execute(
        "INSERT INTO schedule (content_id, platform, planned_at, status) VALUES (?, 'telegram', '2026-06-15 10:00:00', 'planned')",
        (content_id,),
    )

    # Создаём plan_item
    conn.execute(
        "INSERT INTO plan_items (project_id, platform, content_type, date, title, status, content_id) "
        "VALUES (?, 'telegram', 'post', '2026-06-15', 'Пункт', 'generated', ?)",
        (project_id, content_id),
    )
    conn.commit()

    # Создаём временный файл, который должен быть удалён
    data_dir = Path(str(cfg_module.settings.data_dir_absolute))
    videos_dir = data_dir / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    fake_video = videos_dir / f"{content_id}.mp4"
    fake_video.write_text("fake video")

    conn.close()

    # Удаляем проект
    resp = client.post(f"/projects/{slug}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"

    # Проект исчез
    resp = client.get(f"/projects/{slug}")
    assert resp.status_code == 404

    # Контент, schedule, plan_item удалены из БД
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    conn2.execute("PRAGMA foreign_keys=ON")
    assert conn2.execute("SELECT id FROM content WHERE id=?", (content_id,)).fetchone() is None
    assert conn2.execute("SELECT id FROM schedule WHERE content_id=?", (content_id,)).fetchone() is None
    assert conn2.execute("SELECT id FROM plan_items WHERE project_id=?", (project_id,)).fetchone() is None
    conn2.close()

    # Файл удалён
    assert not fake_video.exists()


def test_archive_and_unarchive(client):
    """Архивация и разархивация меняют статус проекта."""
    slug = create_project(client, name="Архивация тест")

    # Архивируем
    resp = client.post(f"/projects/{slug}/archive", follow_redirects=False)
    assert resp.status_code == 303

    detail = client.get(f"/projects/{slug}")
    assert "архив" in detail.text.lower()

    # Разархивируем
    resp = client.post(f"/projects/{slug}/unarchive", follow_redirects=False)
    assert resp.status_code == 303

    detail = client.get(f"/projects/{slug}")
    # После разархивации статус "active" — не должно быть метки архива в шапке
    # (проверяем косвенно: кнопка "Архивировать" снова есть)
    assert "Архивировать" in detail.text


def test_create_project_no_name_returns_422(client):
    """Создание без имени возвращает форму с ошибкой."""
    resp = client.post("/projects/new", data={"name": ""}, follow_redirects=False)
    assert resp.status_code == 422
    assert "обязательно" in resp.text.lower()


def test_dashboard_shows_counts(client):
    """Дашборд отображает корректные счётчики."""
    create_project(client, name="Проект 1")
    create_project(client, name="Проект 2")

    resp = client.get("/")
    assert resp.status_code == 200
    assert "2" in resp.text  # 2 активных проекта


def test_llm_fake_ideas(patch_env):
    """FAKE_LLM=1 → purpose='ideas' возвращает валидный JSON-массив из 5 идей."""
    from app.llm import chat
    result = chat([{"role": "user", "content": "test"}], purpose="ideas")
    ideas = json.loads(result)
    assert isinstance(ideas, list)
    assert len(ideas) == 5
    assert all("text" in idea for idea in ideas)


def test_llm_fake_script(patch_env):
    """FAKE_LLM=1 → purpose='script' возвращает JSON со всеми платформами и features."""
    from app.llm import chat
    result = chat([{"role": "user", "content": "test"}], purpose="script")
    data = json.loads(result)
    assert "telegram" in data
    assert "vk" in data
    assert "youtube" in data
    assert "instagram" in data
    assert "dzen" in data
    assert "features" in data
    assert "hook_type" in data["features"]


def test_llm_fake_default(patch_env):
    """FAKE_LLM=1 → без purpose возвращает строку-заглушку."""
    from app.llm import chat
    result = chat([{"role": "user", "content": "test"}])
    assert result == "FAKE_LLM response"
