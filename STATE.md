# STATE.md — состояние работы (обновляется каждой сессией)

## Текущая точка

- **Этап:** 0 ✅, 1 (LLM-Router) ✅, **ВОЛНА 1 ✅**, **ВОЛНА 2 ✅**, **ВОЛНА 3 ✅**, **ВОЛНА 4 ✅**
- **Сделано (2026-06-10, волна 1):**
  - Структура проекта: `app/`, `tests/`, `data/`, `requirements.txt`, `.env.example`, `.gitignore` обновлён
  - `app/config.py`, `app/db.py`, `app/security.py`, `app/llm.py` — базовый стек
  - `app/web/dashboard.py`, `app/web/projects.py` — CRUD проектов и площадок
  - Шаблоны Jinja2+HTMX, статика, промпты/.gitkeep
- **Сделано (2026-06-10, волна 2 — публикация, параллельный агент):**
  - `app/publishers/`, `app/services/scheduling.py`, `app/scheduler.py`, `app/main.py` (lifespan)
  - `tests/test_publishers.py` → 7 passed ✅
- **Сделано (2026-06-10, волна 2 — pipeline/генерация, этот агент):**
  - `app/prompts/ideas.txt` — русскоязычный промпт с ролью, профилем проекта, learnings, защитой от повторов, few-shot JSON-примером
  - `app/prompts/script.txt` — промпт сценария с адаптацией по площадкам, хуком, телом, CTA; спецификации 5 платформ
  - `app/pipeline/prompts.py` — загрузчик `load_prompt(name, **kwargs)`, безопасные плейсхолдеры <<VAR>>
  - `app/pipeline/ideas.py` — `generate_ideas(project_id, n)`: сбор контекста → LLM → устойчивый парсинг (```-фенсы, 1 retry) → БД
  - `app/pipeline/script.py` — `generate_script(idea_id)`: профиль + включённые площадки → LLM → валидация структуры (1 retry) → content(text_review) + idea(used)
  - `app/web/generate.py` — HTMX-роуты: генерация/ручное добавление идей, «Сделать пост», «Отклонить»; LLMError → человекочитаемые сообщения
  - `app/web/projects.py` — аддитивно: ideas + draft_count в project_detail
  - `app/templates/projects/_ideas_list.html` — HTMX-фрагмент: список идей, бейдж черновиков, кнопки
  - `app/templates/projects/detail.html` — аддитивно: секция «Идеи» с кнопкой генерации + indic. загрузки
  - `app/main.py` — аддитивно: `from app.web.generate import router as generate_router` + `include_router`
  - `tests/test_pipeline.py` — 16 тестов, все ✅
- **Сделано (2026-06-10, фикс тестов):**
  - `app/config.py` — `Settings` переписан: класс-атрибуты заменены на `__getattribute__`+`__setattr__` с двухуровневым lookup (_overrides → os.environ → default). Теперь и `monkeypatch.setenv`, и `monkeypatch.setattr` работают без замены синглтона.
  - `app/web/projects.py` — выделен `_build_detail_context(slug)`, единый хелпер для рендера detail.html; `project_delete` (409-путь) теперь использует хелпер — `ideas` и `draft_count` всегда передаются.
  - `app/templates/projects/_ideas_list.html` — `draft_count | default(0)` — защита от UndefinedError.
  - `tests/test_pipeline.py` — удалена обходная фикстура `ensure_fake_llm` (больше не нужна).
  - `tests/conftest.py` — убрано `cfg_module.settings = cfg_module.Settings()` (тоже не нужно).
- **Сделано (2026-06-10, волна 3 — дашборд ревью и планирования):**
  - `app/web/review.py` — очередь ревью (`/review`): список по всем проектам/фильтр; детальная страница с редактируемыми textarea+счётчиком символов; `approve` (→approved + опциональное планирование на несколько площадок); `reject` (→rejected + learning source='reject')
  - `app/web/calendar.py` — календарь (`/calendar`): вид месяц (сетка 7xN) и неделя; цвет по статусу; перенос/отмена/retry; боковая панель «Готово к планированию»
  - `app/web/manual.py` — ручная очередь (`/manual`): manual_pending → копировать текст (vanilla JS clipboard) + URL → mark_manual_done
  - `app/web/dashboard.py` — обновлён: 6 счётчиков (проекты, ревью, к планированию, 7 дней, ошибки, ручная очередь) + блок «Последние ошибки»
  - `app/templates_env.py` — единый Jinja2Templates с `get_nav_counts()` как глобалом (бейджи в навигации без дублирования)
  - `app/web/nav.py` — `get_nav_counts()`: ревью + manual_pending из БД
  - `app/templates/base.html` — навигация: +Ревью (бейдж), +Календарь, +Ручная очередь (бейдж); через `{% set _nav = get_nav_counts() %}`
  - Все модули web переведены на `from app.templates_env import templates` (единый экземпляр)
  - Шаблоны: `review/list.html`, `review/detail.html`, `calendar/month.html`, `calendar/week.html`, `calendar/entry.html`, `manual/list.html`
  - `tests/test_dashboard.py` — 23 теста покрывают все флоу
  - `app/main.py` — аддитивно: include_router(review/calendar/manual)
- **Сделано (2026-06-10, волна 4 — e2e-тесты + DEPLOY.md завода):**
  - `tests/test_e2e.py` — 3 сценария (A/B/C), полный прогон `pytest tests -q` → **64 passed** ✅ (61 старых + 3 e2e)
  - Сценарий A: создание проекта → площадки с Fernet-шифрованием → генерация идей → пост → ревью + правка → approve + планирование в прошлом → process_due → telegram published (dry-run, outbox-файл) + dzen manual_pending → manual_done с URL → dashboard + calendar.
  - Сценарий B: reject с причиной → learning(source='reject') в БД → patch ideas_module.chat → learning попадает в промпт следующей генерации.
  - Сценарий C: PUBLISH_DRY_RUN=0 + monkeypatch httpx → 3 тика attempts 0→1→2→3, статусы planned/planned/planned/error, retry-роут сбрасывает в planned.
  - `DEPLOY.md` — копипаст-инструкция деплоя завода в том же стиле, что LLMRouter/DEPLOY.md: /opt/kontentzavod, порт 8200, user kontentzavod, MemoryMax=768M (комментарий про 1.5GB на этапе 3 ffmpeg), basic_auth в Caddy (предупреждение + caddy hash-password), FERNET_KEY предупреждение при смене, PUBLISH_DRY_RUN=1 до токенов, ENABLE_SCHEDULER=1 на проде, FAKE_LLM=0.
- **Результат:** `pytest tests -q` → **64 passed** ✅
- **Смоук:** `/` `/review` `/calendar` `/manual` → 200 ✅
- **Запуск завода:** из `A:\DevAI\Projects\KontentZavod`: `.venv\Scripts\uvicorn app.main:app --reload`
- **Запуск роутера:** из `A:\DevAI\Projects\LLMRouter`: `.venv\Scripts\python -m uvicorn app.main:app --port 8100`
- **Следующий шаг:** Волна 5 — видеоконвейер (§3.1 TTS edge-tts, §3.2 ассеты Pexels/Pixabay, §3.3 рендер ffmpeg)

## Что нужно от пользователя (блокеры)

- ~~DeepSeek-ключ~~ ✅ лежит в `.env` (DEEPSEEK_API_KEY) и добавлен в роутер. Пользователь обещал перевыпустить ключ (он засветился в чате) — при 401 спросить новый.
- Free-ключи (Gemini aistudio.google.com / Groq console.groq.com / OpenRouter) — пользователь может добавлять сам через кабинет http://127.0.0.1:8100/admin (пароль changeme — сменить в .env роутера!).
- Данные своих проектов пользователь введёт САМ через дашборд `/projects` (профиль: описание, ЦА, тон, цели, CTA, темы).
- Деплой роутера: пользователь выполняет сам по `LLMRouter\DEPLOY.md` (после деплоя: сменить ADMIN_PASSWORD, перевыпустить виртуальные ключи).
- Токены TG-бота / VK-сообщества — ОТЛОЖЕНО по решению пользователя: пока всё проверяем e2e без реальных API (FAKE_LLM + dry-run паблишеры), реальные токены — после полной реализации.

## Нюансы и грабли (накапливаем)

- **Схема работы:** оркестратор + сабагенты. Кастомные агенты из `.claude/agents` видны только сессиям, начатым ПОСЛЕ их создания; если тип не найден — `general-purpose` с параметром model (haiku/sonnet/opus).
- PowerShell 5.1 читает `.ps1` без BOM как ANSI → в скриптах хуков только ASCII. Execution policy = Restricted → хук запускается через `Get-Content ... | Invoke-Expression` (см. settings.json).
- Starlette 1.2.x: `TemplateResponse(request, name, ctx)` — request первым аргументом (в admin.py обёртка `_tmpl`).
- SQLite `SUM(CASE...)` на пустой таблице даёт None → COALESCE.
- DeepSeek в финальном SSE-чанке отдаёт `usage` — парсится для подсчёта токенов в stream-режиме.
- Кракозябры в выводе Invoke-RestMethod — особенность консоли PS 5.1, JSON роутера в норме (UTF-8).
- **Долг (мелочи, поправить при случае):** (1) в кабинете полный vk-ключ передаётся через redirect URL → попадает в access-логи uvicorn, лучше через одноразовую сессию; (2) manage.py дублирует часть логики services.py — свести к одному источнику.
- **FAKE_LLM-ловушка (РЕШЕНА):** `Settings` переписан на `__getattribute__`+`__setattr__` с двухуровневым lookup. Теперь `monkeypatch.setenv("FAKE_LLM", "1")` из `conftest.patch_env` работает во ВСЕХ тест-файлах без дополнительных хаков. `monkeypatch.setattr(settings, "PUBLISH_DRY_RUN", True)` тоже работает (сохраняется в `_overrides`). НЕ возвращать class-level атрибуты в config.py.
- Тесты до фикса успели намусорить в продовой `data/kontentzavod.db` и `data/outbox/` — оркестратор очистил (2026-06-10), БД пересоздаётся на старте. Полный pytest 38/38 подтверждён оркестратором.

- Сервер: макс 4GB RAM / 2 vCPU / 30GB SSD → никаких Postgres/Redis/Celery/n8n/moviepy. SQLite + APScheduler + чистый ffmpeg.
- У пользователя RX 580 (AMD) — локальная генерация невозможна, всё через API.
- Платный бюджет: только DeepSeek API. Остальное — free-тиры и бесплатные сервисы.
- Браузер-автоматизация: отложена, только этап 6, только Gemini, только если пользователь захочет.
- Контент на русском. Несколько проектов: динамические, в БД, CRUD через дашборд (НЕ projects.yaml). Per-project креденшелы площадок — Fernet в project_platforms.
- Публикация: TG/VK авто; YouTube авто с оговоркой (audit); IG/Дзен — ручная очередь через дашборд.

- **Shared templates (волна 3):** `app/templates_env.py` — единственный экземпляр `Jinja2Templates`. Все web-модули импортируют `from app.templates_env import templates`. Jinja2-глобал `get_nav_counts` вызывается прямо из `base.html` (`{% set _nav = get_nav_counts() %}`). При добавлении нового web-модуля: НЕ создавать локальный `templates`, импортировать из `templates_env`.
- Автопереключение модели между сессиями: поле `"model"` в `.claude/settings.json` проекта (обновляет Claude в конце сессии). Сейчас стоит `sonnet` под этап 1.
- Stop-хук контроля свежести STATE.md заблокирован классификатором разрешений — ждёт решения пользователя (скрипт описан в журнале сессии 2026-06-10).

## Журнал сессий

- **2026-06-10 (1)** — проектирование системы, этап 0: PLAN/STATE/CLAUDE/agents. DeepSeek-ключ в `.env`, `.gitignore`, усилен протокол (обновление STATE/PLAN после каждого цикла).
- **2026-06-10 (2)** — решение пользователя: схема «оркестратор + сабагенты» вместо переключения модели главной сессии (поле model из settings.json убрано). Создан и протестирован Stop-хук свежести STATE.md (пропуск/блок/защита от цикла — все пути проверены; подхватится после перезапуска сессии или /hooks). **Этап 1: LLM-Router построен двумя sonnet-сабагентами и проверен оркестратором реальными вызовами DeepSeek.** Найден и исправлен баг: эндпоинт всегда отдавал SSE → теперь non-stream возвращает единый JSON, стрим не буферируется, токены пишутся. Веб-кабинет: логин, аккаунты (free выделены), ключи, статистика — все проверки прошли. Остался деплой на VPS.
- **2026-06-10 (3)** — **ЭТАП 2 ПОЛНОСТЬЮ ЗАВЕРШЁН** (оркестратор + 6 sonnet-сабагентов, 4 волны). (а) `LLMRouter\DEPLOY.md` — копипаст-деплой роутера (systemd + поддомен в существующий Caddyfile + обновление rsync). (б) Перепроектирование по требованию пользователя: проекты динамические в БД с CRUD через дашборд, projects.yaml отменён, per-project креденшелы Fernet. (в) Волна 1: скелет, 7 таблиц, CRUD проектов/площадок, llm.py+FAKE_LLM. (г) Волна 2 (2 параллельных агента): pipeline ideas/script + промпты ideas.txt/script.txt; publishers TG/VK/manual с dry-run в data/outbox + scheduler. Интеграционный фикс стыка волн: Settings → динамическое чтение env (5 падений устранены в корне). (д) Волна 3: ревью-очередь с правкой и approve+планированием, reject→learnings, календарь месяц/неделя с переносом/retry, ручная очередь, сводка на главной. (е) Волна 4: e2e A/B/C (полный цикл по HTTP, петля обучения reject→промпт, отказоустойчивость 3 ретрая→error→retry) + `DEPLOY.md` завода (порт 8200, basic_auth через Caddy — у дашборда нет своего логина!). Финальная верификация оркестратора: **pytest 64/64 ✅, смоук 5 страниц 200 ✅, продовые data/ чистые**. Нюанс волны 4: `from app.llm import chat` в ideas.py → в тестах патчить `app.pipeline.ideas.chat`, не `app.llm.chat`.
