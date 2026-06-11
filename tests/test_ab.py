"""
Тесты A/B-теста хуков для текстовых постов.
Все тесты работают с FAKE_LLM=1 (задаётся в conftest.patch_env).
"""
import json
import uuid
import pytest

from app.db import get_db, init_db
from app.llm import LLMError
import app.config as cfg_module


# ─── Вспомогательные фикстуры ─────────────────────────────────────────────────


def _unique_slug() -> str:
    return f"ab-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def db_project_ab(patch_env):
    """Создаёт проект с двумя включёнными площадками (telegram, vk)."""
    init_db(cfg_module.settings.db_path_absolute)
    slug = _unique_slug()
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO projects
                (slug, name, description, audience, tone, goals, cta, themes, forbidden, extra)
            VALUES (?, 'AB-проект',
                    'Описание продукта', 'Предприниматели 25-45',
                    'Дружелюбный', 'Рост продаж',
                    'Перейти на сайт', 'Бизнес, маркетинг',
                    'Политика', 'Доп. контекст')
            """,
            (slug,),
        )
        project_id = cur.lastrowid

        for platform in ("telegram", "vk"):
            db.execute(
                "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?, ?, 1, 'auto')",
                (project_id, platform),
            )
        for platform in ("youtube", "instagram", "dzen"):
            db.execute(
                "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?, ?, 0, 'auto')",
                (project_id, platform),
            )
    return project_id


@pytest.fixture
def db_idea_ab(db_project_ab):
    """Создаёт идею для db_project_ab, возвращает (project_id, idea_id)."""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
            (db_project_ab, "Тестовая A/B идея", "manual", "new"),
        )
        idea_id = cur.lastrowid
    return db_project_ab, idea_id


# ─── 1. generate_script_ab базовые тесты ─────────────────────────────────────


class TestGenerateScriptAb:
    def test_creates_two_content_records(self, db_idea_ab):
        """Должны создаться ровно 2 контента в status='text_review'."""
        from app.pipeline.script import generate_script_ab

        project_id, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        assert isinstance(id_a, int)
        assert isinstance(id_b, int)
        assert id_a != id_b

        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM content WHERE project_id=? ORDER BY id",
                (project_id,),
            ).fetchall()

        assert len(rows) == 2

    def test_ab_variant_field(self, db_idea_ab):
        """features.ab_variant должен быть 'A' и 'B' соответственно."""
        from app.pipeline.script import generate_script_ab

        _, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        with get_db() as db:
            row_a = db.execute("SELECT features FROM content WHERE id=?", (id_a,)).fetchone()
            row_b = db.execute("SELECT features FROM content WHERE id=?", (id_b,)).fetchone()

        features_a = json.loads(row_a["features"])
        features_b = json.loads(row_b["features"])

        assert features_a["ab_variant"] == "A"
        assert features_b["ab_variant"] == "B"

    def test_hook_type_differs(self, db_idea_ab):
        """hook_type вариантов A и B должны различаться."""
        from app.pipeline.script import generate_script_ab

        _, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        with get_db() as db:
            row_a = db.execute("SELECT features FROM content WHERE id=?", (id_a,)).fetchone()
            row_b = db.execute("SELECT features FROM content WHERE id=?", (id_b,)).fetchone()

        features_a = json.loads(row_a["features"])
        features_b = json.loads(row_b["features"])

        assert features_a["hook_type"] != features_b["hook_type"]

    def test_texts_differ(self, db_idea_ab):
        """Тексты вариантов A и B должны различаться (разные хуки)."""
        from app.pipeline.script import generate_script_ab

        _, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        with get_db() as db:
            row_a = db.execute("SELECT texts FROM content WHERE id=?", (id_a,)).fetchone()
            row_b = db.execute("SELECT texts FROM content WHERE id=?", (id_b,)).fetchone()

        texts_a = json.loads(row_a["texts"])
        texts_b = json.loads(row_b["texts"])

        # Хотя бы одна площадка должна иметь разный текст
        different = False
        for platform in ("telegram", "vk"):
            if platform in texts_a and platform in texts_b:
                text_a = texts_a[platform].get("text") or texts_a[platform].get("caption", "")
                text_b = texts_b[platform].get("text") or texts_b[platform].get("caption", "")
                if text_a != text_b:
                    different = True
                    break
        assert different, "Тексты вариантов A и B не должны быть идентичны"

    def test_idea_becomes_used(self, db_idea_ab):
        """Идея должна перейти в status='used' после генерации."""
        from app.pipeline.script import generate_script_ab

        _, idea_id = db_idea_ab
        generate_script_ab(idea_id)

        with get_db() as db:
            idea = db.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()
        assert idea["status"] == "used"

    def test_title_has_ab_suffix(self, db_idea_ab):
        """Заголовки должны заканчиваться на ' [A]' и ' [B]'."""
        from app.pipeline.script import generate_script_ab

        _, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        with get_db() as db:
            row_a = db.execute("SELECT title FROM content WHERE id=?", (id_a,)).fetchone()
            row_b = db.execute("SELECT title FROM content WHERE id=?", (id_b,)).fetchone()

        assert row_a["title"].endswith(" [A]")
        assert row_b["title"].endswith(" [B]")

    def test_only_enabled_platforms_in_texts(self, db_idea_ab):
        """texts должен содержать ТОЛЬКО включённые площадки (telegram, vk)."""
        from app.pipeline.script import generate_script_ab

        _, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        with get_db() as db:
            row_a = db.execute("SELECT texts FROM content WHERE id=?", (id_a,)).fetchone()
            row_b = db.execute("SELECT texts FROM content WHERE id=?", (id_b,)).fetchone()

        for row in (row_a, row_b):
            texts = json.loads(row["texts"])
            assert "telegram" in texts
            assert "vk" in texts
            assert "youtube" not in texts
            assert "instagram" not in texts
            assert "dzen" not in texts

    def test_content_type_and_status(self, db_idea_ab):
        """Оба контента должны быть type='post', status='text_review'."""
        from app.pipeline.script import generate_script_ab

        project_id, idea_id = db_idea_ab
        id_a, id_b = generate_script_ab(idea_id)

        with get_db() as db:
            for cid in (id_a, id_b):
                row = db.execute("SELECT type, status FROM content WHERE id=?", (cid,)).fetchone()
                assert row["type"] == "post"
                assert row["status"] == "text_review"


# ─── 2. Retry и ошибки ───────────────────────────────────────────────────────


class TestGenerateScriptAbRetry:
    def test_invalid_first_valid_second_succeeds(self, db_idea_ab, monkeypatch):
        """Первый ответ невалидный → retry → валидный → 2 контента созданы."""
        from app.pipeline import script as script_module
        import app.llm as llm_module

        call_count = 0

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "это не json вообще"
            return llm_module._FAKE_SCRIPT_AB

        monkeypatch.setattr(script_module, "chat", mock_chat)
        _, idea_id = db_idea_ab

        id_a, id_b = script_module.generate_script_ab(idea_id)

        assert call_count == 2
        assert id_a > 0
        assert id_b > 0

    def test_invalid_twice_raises_llm_error_no_content_created(self, db_idea_ab, monkeypatch):
        """Два невалидных ответа подряд → LLMError, контент не создан."""
        from app.pipeline import script as script_module

        project_id, idea_id = db_idea_ab
        call_count = 0

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            nonlocal call_count
            call_count += 1
            return "это не json вообще"

        monkeypatch.setattr(script_module, "chat", mock_chat)

        with pytest.raises(LLMError):
            script_module.generate_script_ab(idea_id)

        assert call_count == 2

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM content WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        assert count == 0

    def test_same_hook_type_triggers_retry(self, db_idea_ab, monkeypatch):
        """Ответ с одинаковыми hook_type у A и B → ошибка валидации → retry."""
        from app.pipeline import script as script_module
        import app.llm as llm_module
        import json as json_mod

        call_count = 0
        invalid_ab = json_mod.loads(llm_module._FAKE_SCRIPT_AB)
        # Сделать оба варианта с одинаковым hook_type
        invalid_ab["variants"]["B"]["hook_type"] = invalid_ab["variants"]["A"]["hook_type"]
        invalid_ab_str = json_mod.dumps(invalid_ab, ensure_ascii=False)

        def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return invalid_ab_str
            return llm_module._FAKE_SCRIPT_AB

        monkeypatch.setattr(script_module, "chat", mock_chat)
        _, idea_id = db_idea_ab

        id_a, id_b = script_module.generate_script_ab(idea_id)
        assert call_count == 2
        assert id_a != id_b


# ─── 3. Веб-роут /ideas/{id}/script_ab ───────────────────────────────────────


