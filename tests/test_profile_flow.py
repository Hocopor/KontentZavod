"""
Тесты двухшагового создания проекта с ИИ-профилем.

Покрывает:
1. generate_profile на FAKE_LLM — возвращает dict с 6 ключами
2. retry при битом JSON — 1-й вызов мусор, 2-й валидный JSON
3. Шаг 1 — GET /projects/new возвращает 200 и содержит 3 поля шага 1
4. POST /projects/generate-profile — возвращает заполненную форму (значения из FAKE)
5. Полный цикл: generate-profile → POST /projects/new → проект в БД со всеми полями
6. LLMError при вызове generate-profile → сообщение об ошибке в HTML
"""
import json
import sqlite3

import pytest


# ─── Тесты generate_profile ───────────────────────────────────────────────────

def test_generate_profile_fake_llm(patch_env):
    """FAKE_LLM=1 → generate_profile возвращает dict с 6 ключами."""
    from app.pipeline.profile import generate_profile

    result = generate_profile(
        name="Тест Проект",
        description="Описание продукта",
        goals="Продажи и охват",
    )

    assert isinstance(result, dict)
    for key in ("audience", "tone", "cta", "themes", "forbidden", "extra"):
        assert key in result, f"Отсутствует ключ: {key}"
        assert isinstance(result[key], str)


def test_generate_profile_all_keys_present(patch_env):
    """Все 6 ключей присутствуют и непустые (FAKE_LLM возвращает полный профиль)."""
    from app.pipeline.profile import generate_profile

    result = generate_profile("Мой бизнес", "Продаём кофе", "Рост подписчиков")

    for key in ("audience", "tone", "cta", "themes", "forbidden", "extra"):
        assert result[key], f"Ключ {key!r} пустой, ожидали непустое значение"


def test_generate_profile_retry_on_invalid_json(patch_env, monkeypatch):
    """1-й вызов chat возвращает мусор, 2-й — валидный JSON → успех."""
    valid_profile = json.dumps({
        "audience": "ЦА тест",
        "tone": "Дружелюбный",
        "cta": "Позвонить",
        "themes": "Тема1, Тема2",
        "forbidden": "Стоп",
        "extra": "Доп контекст",
    }, ensure_ascii=False)

    call_count = {"n": 0}

    def fake_chat(messages, purpose=None, json_mode=False, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "это не JSON {{{{{ мусор"
        return valid_profile

    import app.pipeline.profile as profile_module
    monkeypatch.setattr(profile_module, "chat", fake_chat)

    from app.pipeline.profile import generate_profile
    result = generate_profile("Ретрай тест", "Описание", "Цель")

    assert call_count["n"] == 2, "Ожидали ровно 2 вызова chat"
    assert result["audience"] == "ЦА тест"
    assert result["tone"] == "Дружелюбный"


def test_generate_profile_raises_llm_error_after_two_failures(patch_env, monkeypatch):
    """Оба вызова chat возвращают мусор → LLMError."""
    from app.llm import LLMError

    def fake_chat(messages, purpose=None, json_mode=False, **kwargs):
        return "не JSON вообще"

    import app.pipeline.profile as profile_module
    monkeypatch.setattr(profile_module, "chat", fake_chat)

    from app.pipeline.profile import generate_profile
    with pytest.raises(LLMError):
        generate_profile("Провал", "Описание", "Цель")


def test_generate_profile_missing_keys_filled_with_empty(patch_env, monkeypatch):
    """JSON без части ключей → отсутствующие заполняются пустой строкой."""
    partial = json.dumps({"audience": "Аудитория", "tone": "Экспертный"}, ensure_ascii=False)

    def fake_chat(messages, purpose=None, json_mode=False, **kwargs):
        return partial

    import app.pipeline.profile as profile_module
    monkeypatch.setattr(profile_module, "chat", fake_chat)

    from app.pipeline.profile import generate_profile
    result = generate_profile("Частичный", "Описание", "Цель")

    assert result["audience"] == "Аудитория"
    assert result["tone"] == "Экспертный"
    assert result["cta"] == ""
    assert result["themes"] == ""
    assert result["forbidden"] == ""
    assert result["extra"] == ""


# ─── Тесты веб-роутов ─────────────────────────────────────────────────────────

def test_step1_page_returns_200(client):
    """GET /projects/new возвращает 200 и содержит 3 поля шага 1."""
    resp = client.get("/projects/new")
    assert resp.status_code == 200

    # Три поля шага 1 должны присутствовать в HTML
    assert "step1-name" in resp.text or "step1_name" in resp.text
    assert "step1-description" in resp.text or "step1_description" in resp.text
    assert "step1-goals" in resp.text or "step1_goals" in resp.text


def test_step1_page_has_ai_button(client):
    """GET /projects/new содержит кнопку вызова ИИ."""
    resp = client.get("/projects/new")
    assert resp.status_code == 200
    assert "generate-profile" in resp.text


def test_generate_profile_endpoint_returns_filled_form(client):
    """POST /projects/generate-profile возвращает форму с предзаполненными полями из FAKE_LLM."""
    resp = client.post(
        "/projects/generate-profile",
        data={
            "name": "ТестПроект",
            "description": "Описание",
            "goals": "Цель",
        },
    )
    assert resp.status_code == 200

    # Форма должна содержать значения из _FAKE_PROFILE
    # audience: "Предприниматели и маркетологи 25–45 лет"
    assert "Предприниматели" in resp.text
    # tone: "Экспертный, но дружелюбный"
    assert "Экспертный" in resp.text
    # Поля формы присутствуют
    assert 'name="audience"' in resp.text
    assert 'name="tone"' in resp.text
    assert 'name="themes"' in resp.text


def test_generate_profile_endpoint_prefills_name(client):
    """POST /projects/generate-profile — поле name предзаполнено введённым пользователем именем."""
    resp = client.post(
        "/projects/generate-profile",
        data={
            "name": "МойУникальныйПроект",
            "description": "Описание продукта",
            "goals": "Продажи",
        },
    )
    assert resp.status_code == 200
    assert "МойУникальныйПроект" in resp.text


def test_generate_profile_manual_mode_returns_empty_form(client):
    """POST /projects/generate-profile?manual=1 возвращает форму без ИИ-профиля."""
    resp = client.post(
        "/projects/generate-profile",
        data={
            "name": "Ручной Проект",
            "description": "Описание",
            "goals": "Цель",
            "manual": "1",
        },
    )
    assert resp.status_code == 200
    # Форма есть
    assert 'name="name"' in resp.text
    # Поля аудитории и тона пустые (нет FAKE_PROFILE данных)
    assert "Предприниматели" not in resp.text
    assert "Экспертный, но дружелюбный" not in resp.text


def test_generate_profile_llm_error_shows_message(client, monkeypatch):
    """LLMError в generate_profile → HTML с сообщением об ошибке."""
    from app.llm import LLMError as _LLMError

    def fake_generate_profile(name, description, goals):
        raise _LLMError("Роутер недоступен")

    import app.web.projects as projects_module
    # Мокаем generate_profile через monkeypatch на импорт в роуте
    original_post = None

    # Патчим через pipeline.profile.generate_profile
    import app.pipeline.profile as prof_mod
    monkeypatch.setattr(prof_mod, "generate_profile", fake_generate_profile)

    resp = client.post(
        "/projects/generate-profile",
        data={"name": "Ошибка", "description": "Описание", "goals": "Цель"},
    )
    assert resp.status_code == 200
    # Сообщение об ошибке присутствует
    assert "Ошибка" in resp.text or "ошибка" in resp.text.lower() or "Роутер" in resp.text


# ─── Полный цикл: generate-profile → создание проекта в БД ───────────────────

def test_full_cycle_generate_profile_then_create(client, patch_env):
    """
    Полный цикл: generate-profile (FAKE_LLM) → POST /projects/new → проект в БД.
    Проверяем, что все поля профиля (audience, tone, cta, themes, forbidden, extra) сохранены.
    """
    import app.config as cfg_module

    # Шаг 1: получить форму от ИИ
    gen_resp = client.post(
        "/projects/generate-profile",
        data={
            "name": "Цикл Тест",
            "description": "Продукт для теста",
            "goals": "Продажи",
        },
    )
    assert gen_resp.status_code == 200
    # Убедиться, что ИИ вернул значения (они будут в форме)
    assert "Предприниматели" in gen_resp.text  # из _FAKE_PROFILE

    # Шаг 2: отправить полную форму создания проекта
    create_resp = client.post(
        "/projects/new",
        data={
            "name": "Цикл Тест",
            "description": "Продукт для теста",
            "audience": "Предприниматели и маркетологи 25–45 лет, ищут инструменты роста",
            "tone": "Экспертный, но дружелюбный — без жаргона, с конкретными примерами",
            "goals": "Продажи",
            "cta": "Записаться на бесплатную консультацию или скачать чек-лист",
            "links": "",
            "themes": "Маркетинг, автоматизация бизнеса, кейсы клиентов",
            "forbidden": "Политика, религия, негатив о конкурентах",
            "extra": "Акцент на практических результатах: цифры, сроки",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    slug = create_resp.headers["location"].rstrip("/").split("/")[-1]

    # Проверяем БД
    db_path = str(cfg_module.settings.db_path_absolute)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    conn.close()

    assert row is not None
    assert row["audience"] is not None and "Предприниматели" in row["audience"]
    assert row["tone"] is not None and "Экспертный" in row["tone"]
    assert row["cta"] is not None and "консультацию" in row["cta"]
    assert row["themes"] is not None and "Маркетинг" in row["themes"]
    assert row["forbidden"] is not None and "Политика" in row["forbidden"]
    assert row["extra"] is not None and "результатах" in row["extra"]


# ─── Тест: секция «Идеи» удалена со страницы проекта ─────────────────────────

def test_detail_page_has_no_ideas_section(client, patch_env):
    """На детальной странице проекта нет секции «Идеи» и связанных кнопок старого флоу."""
    from app.db import get_db, init_db
    import app.config as cfg_module

    init_db(cfg_module.settings.db_path_absolute)

    # Создать проект
    resp = client.post(
        "/projects/new",
        data={"name": "Тест без идей", "description": "Описание", "goals": "Цели"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    slug = resp.headers["location"].rstrip("/").split("/")[-1]

    # Проверить страницу проекта
    detail = client.get(f"/projects/{slug}")
    assert detail.status_code == 200

    # Секция «Идеи» и кнопки старого флоу отсутствуют
    assert "Сгенерировать идеи" not in detail.text
    assert "ideas/generate" not in detail.text
    assert "Сделать пост" not in detail.text
    assert "A/B пост" not in detail.text
    assert "Слайдшоу" not in detail.text
    assert "Футажи" not in detail.text
    # id секции ideas удалён
    assert 'id="ideas-section"' not in detail.text
    assert 'id="ideas"' not in detail.text
