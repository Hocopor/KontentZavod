"""
Тесты pipeline: генерация идей, сценариев, веб-роуты.
Все тесты работают с FAKE_LLM=1 (задаётся в conftest.patch_env).
"""
import json
import uuid
import pytest

from app.db import get_db, init_db
from app.llm import LLMError
import app.config as cfg_module


# ─── Примечание о FAKE_LLM ────────────────────────────────────────────────────
#
# config.Settings теперь читает os.environ через @property при каждом обращении.
# monkeypatch.setenv("FAKE_LLM", "1") из conftest.patch_env (autouse=True)
# достаточно — все модули (llm, db и др.), держащие ссылку на singleton settings,
# автоматически видят актуальное значение без дополнительного патчинга.
#
# Фикстура ensure_fake_llm больше не нужна и удалена.


# ─── Вспомогательные фикстуры ─────────────────────────────────────────────────


def _unique_slug() -> str:
    """Уникальный slug для изолированных тестов."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def db_project(patch_env):
    """Создаёт проект и площадки, возвращает project_id."""
    init_db(cfg_module.settings.db_path_absolute)
    slug = _unique_slug()
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO projects
                (slug, name, description, audience, tone, goals, cta, themes, forbidden, extra)
            VALUES (?, 'Тестовый проект',
                    'Описание продукта', 'Предприниматели 25-45',
                    'Дружелюбный', 'Рост продаж',
                    'Перейти на сайт', 'Бизнес, маркетинг',
                    'Политика, религия', 'Доп. контекст')
            """,
            (slug,),
        )
        project_id = cur.lastrowid

        # Включить две площадки
        for platform in ("telegram", "vk"):
            db.execute(
                """
                INSERT INTO project_platforms (project_id, platform, enabled, mode)
                VALUES (?, ?, 1, 'auto')
                """,
                (project_id, platform),
            )
        # Остальные — выключены
        for platform in ("youtube", "instagram", "dzen"):
            db.execute(
                """
                INSERT INTO project_platforms (project_id, platform, enabled, mode)
                VALUES (?, ?, 0, 'auto')
                """,
                (project_id, platform),
            )

    return project_id


@pytest.fixture
def db_idea(db_project):
    """Создаёт идею для db_project, возвращает (project_id, idea_id)."""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
            (db_project, "Тестовая идея для поста", "manual", "new"),
        )
        idea_id = cur.lastrowid
    return db_project, idea_id


# ─── 1. generate_ideas ────────────────────────────────────────────────────────


class TestGenerateIdeas:
    def test_creates_n_ideas_in_db(self, db_project):
        from app.pipeline.ideas import generate_ideas

        ids = generate_ideas(db_project, n=5)

        assert len(ids) == 5
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM ideas WHERE project_id=? AND source='generated' AND status='new'",
                (db_project,),
            ).fetchall()
        assert len(rows) == 5
        # Все возвращённые id действительно есть в БД
        db_ids = {r["id"] for r in rows}
        assert set(ids) == db_ids

    def test_prompt_contains_project_fields(self, db_project, monkeypatch):
        """Проверяем, что в промпт попали ЦА, тон и стоп-темы."""
        from app.pipeline import ideas as ideas_module
        import app.llm as llm_module

        captured: list[list[dict]] = []

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            captured.append(messages)
            return llm_module._FAKE_IDEAS

        monkeypatch.setattr(ideas_module, "chat", mock_chat)
        ideas_module.generate_ideas(db_project, n=3)

        assert captured, "chat не был вызван"
        prompt_text = captured[0][0]["content"]

        assert "Предприниматели 25-45" in prompt_text, "ЦА не попала в промпт"
        assert "Дружелюбный" in prompt_text, "Тон не попал в промпт"
        assert "Политика, религия" in prompt_text, "Стоп-темы не попали в промпт"

    def test_prompt_contains_recent_topics(self, db_project, monkeypatch):
        """Уже существующие идеи должны попасть в промпт как использованные темы."""
        with get_db() as db:
            db.execute(
                "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
                (db_project, "Старая тема которую не надо повторять", "manual", "used"),
            )

        from app.pipeline import ideas as ideas_module
        import app.llm as llm_module

        captured: list[list[dict]] = []

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            captured.append(messages)
            return llm_module._FAKE_IDEAS

        monkeypatch.setattr(ideas_module, "chat", mock_chat)
        ideas_module.generate_ideas(db_project, n=2)

        prompt_text = captured[0][0]["content"]
        assert "Старая тема которую не надо повторять" in prompt_text

    def test_archived_project_raises(self, db_project):
        from app.pipeline.ideas import generate_ideas

        with get_db() as db:
            db.execute(
                "UPDATE projects SET status='archived' WHERE id=?", (db_project,)
            )

        with pytest.raises(ValueError, match="заархивирован"):
            generate_ideas(db_project)

    def test_invalid_json_twice_raises_llm_error(self, db_project, monkeypatch):
        """Два невалидных ответа подряд → LLMError."""
        from app.pipeline import ideas as ideas_module

        call_count = 0

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            nonlocal call_count
            call_count += 1
            return "это не json вообще"

        monkeypatch.setattr(ideas_module, "chat", mock_chat)

        with pytest.raises(LLMError):
            ideas_module.generate_ideas(db_project, n=2)

        assert call_count == 2, "Должно быть ровно 2 попытки"

    def test_json_with_fences_parsed(self, db_project, monkeypatch):
        """Ответ с ```json-фенсами должен парситься корректно."""
        from app.pipeline import ideas as ideas_module

        fenced = '```json\n[{"text": "Идея в фенсах"}]\n```'

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            return fenced

        monkeypatch.setattr(ideas_module, "chat", mock_chat)
        ids = ideas_module.generate_ideas(db_project, n=1)
        assert len(ids) == 1

        with get_db() as db:
            row = db.execute("SELECT text FROM ideas WHERE id=?", (ids[0],)).fetchone()
        assert row["text"] == "Идея в фенсах"


# ─── 2. generate_script ───────────────────────────────────────────────────────


class TestGenerateScript:
    def test_content_created_with_correct_fields(self, db_idea):
        from app.pipeline.script import generate_script

        project_id, idea_id = db_idea
        content_id = generate_script(idea_id)

        assert isinstance(content_id, int)
        with get_db() as db:
            content = db.execute(
                "SELECT * FROM content WHERE id=?", (content_id,)
            ).fetchone()

        assert content is not None
        assert content["type"] == "post"
        assert content["status"] == "text_review"
        assert content["project_id"] == project_id
        assert content["idea_id"] == idea_id

    def test_texts_only_enabled_platforms(self, db_idea):
        """texts должен содержать ТОЛЬКО включённые площадки (telegram, vk)."""
        from app.pipeline.script import generate_script

        _, idea_id = db_idea
        content_id = generate_script(idea_id)

        with get_db() as db:
            content = db.execute(
                "SELECT texts FROM content WHERE id=?", (content_id,)
            ).fetchone()

        texts = json.loads(content["texts"])
        # Только включённые
        assert "telegram" in texts
        assert "vk" in texts
        # Выключенные — не должны быть
        assert "youtube" not in texts
        assert "instagram" not in texts
        assert "dzen" not in texts

    def test_features_valid(self, db_idea):
        """features должен содержать все обязательные ключи."""
        from app.pipeline.script import generate_script

        _, idea_id = db_idea
        content_id = generate_script(idea_id)

        with get_db() as db:
            content = db.execute(
                "SELECT features FROM content WHERE id=?", (content_id,)
            ).fetchone()

        features = json.loads(content["features"])
        for key in ("hook_type", "topic", "length", "format"):
            assert key in features, f"features.{key} отсутствует"

    def test_idea_becomes_used(self, db_idea):
        from app.pipeline.script import generate_script

        _, idea_id = db_idea
        generate_script(idea_id)

        with get_db() as db:
            idea = db.execute(
                "SELECT status FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
        assert idea["status"] == "used"

    def test_invalid_json_twice_raises_llm_error(self, db_idea, monkeypatch):
        from app.pipeline import script as script_module

        call_count = 0

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            nonlocal call_count
            call_count += 1
            return "не json"

        monkeypatch.setattr(script_module, "chat", mock_chat)
        _, idea_id = db_idea

        with pytest.raises(LLMError):
            script_module.generate_script(idea_id)

        assert call_count == 2

    def test_json_with_fences_parsed(self, db_idea, monkeypatch):
        """Ответ сценария с ```-фенсами должен парситься."""
        from app.pipeline import script as script_module
        import app.llm as llm_module

        fenced = f"```json\n{llm_module._FAKE_SCRIPT}\n```"

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            return fenced

        monkeypatch.setattr(script_module, "chat", mock_chat)
        _, idea_id = db_idea
        content_id = script_module.generate_script(idea_id)
        assert content_id > 0


# ─── 3. Веб-роуты ─────────────────────────────────────────────────────────────


class TestWebRoutes:
    def _create_project(self, client) -> str:
        """Создаёт проект через веб и возвращает slug."""
        name = f"Веб-тест {_unique_slug()}"
        resp = client.post(
            "/projects/new",
            data={
                "name": name,
                "description": "Описание",
                "audience": "Тестовая аудитория",
                "tone": "Нейтральный",
                "goals": "Тестирование",
                "cta": "Нажми сюда",
                "themes": "Тема1, Тема2",
                "forbidden": "Запрет",
                "extra": "",
            },
            follow_redirects=False,
        )
        location = resp.headers.get("location", "")
        slug = location.rstrip("/").split("/")[-1]
        return slug

    def test_generate_ideas_returns_200_and_ideas_in_db(self, client):
        slug = self._create_project(client)

        with get_db() as db:
            proj = db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        project_id = proj["id"]

        resp = client.post(f"/projects/{slug}/ideas/generate")
        assert resp.status_code == 200

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM ideas WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        assert count > 0

    def test_manual_idea_added(self, client):
        slug = self._create_project(client)

        resp = client.post(
            f"/projects/{slug}/ideas/add",
            data={"text": "Ручная тестовая идея"},
        )
        assert resp.status_code == 200

        with get_db() as db:
            proj = db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
            idea = db.execute(
                "SELECT * FROM ideas WHERE project_id=? AND source='manual'",
                (proj["id"],),
            ).fetchone()

        assert idea is not None
        assert idea["text"] == "Ручная тестовая идея"
        assert idea["status"] == "new"

    def test_generate_ideas_for_unknown_project(self, client):
        resp = client.post("/projects/nonexistent-slug/ideas/generate")
        assert resp.status_code == 404

    def test_script_creates_content(self, client):
        slug = self._create_project(client)

        with get_db() as db:
            proj = db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
            project_id = proj["id"]
            for platform in ("telegram", "vk"):
                db.execute(
                    """
                    INSERT OR REPLACE INTO project_platforms (project_id, platform, enabled, mode)
                    VALUES (?, ?, 1, 'auto')
                    """,
                    (project_id, platform),
                )

        with get_db() as db:
            cur = db.execute(
                "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
                (project_id, "Тестовая идея", "manual", "new"),
            )
            idea_id = cur.lastrowid

        resp = client.post(f"/ideas/{idea_id}/script")
        # Либо 200 (HTMX-фрагмент), либо редирект 303
        assert resp.status_code in (200, 303)

        with get_db() as db:
            content = db.execute(
                "SELECT * FROM content WHERE project_id=?", (project_id,)
            ).fetchone()
        assert content is not None
        assert content["status"] == "text_review"
