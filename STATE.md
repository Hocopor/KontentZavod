# STATE.md — состояние работы (обновляется каждой сессией)

## Текущая точка

- **Этап:** 0 ✅, 1 (LLM-Router) ✅, **ВОЛНА 1 КонтентЗавода ✅**, **ВОЛНА 2 (публикация) — частично ✅** (паблишеры + планировщик готовы; pipeline/генерация и UI ручной очереди — параллельный агент).
- **Сделано (2026-06-10, волна 1):**
  - Структура проекта: `app/`, `tests/`, `data/`, `requirements.txt`, `.env.example`, `.gitignore` обновлён
  - `app/config.py` — Settings из .env (DB_PATH, LLM_ROUTER_BASE_URL, LLM_ROUTER_KEY, FERNET_KEY, FAKE_LLM, DATA_DIR)
  - `app/db.py` — sqlite3, WAL, FK ON, init_db (полная схема всех 7 таблиц), get_db контекстный менеджер
  - `app/security.py` — Fernet encrypt/decrypt для токенов площадок
  - `app/llm.py` — клиент LLM-Router (openai SDK), FAKE_LLM-заглушки (ideas/script/default), LLMError
  - `app/web/dashboard.py`, `app/web/projects.py` — CRUD проектов и площадок
  - Шаблоны Jinja2+HTMX, статика, промпты/.gitkeep
- **Сделано (2026-06-10, волна 2 — публикация):**
  - `app/config.py` — добавлены PUBLISH_DRY_RUN (default 1) и ENABLE_SCHEDULER (default 0)
  - `.env`, `.env.example` — дополнены PUBLISH_DRY_RUN=1, ENABLE_SCHEDULER=0
  - `app/publishers/base.py` — PublishError, publish(schedule_id), dry_run_publish(), outbox в data/outbox/
  - `app/publishers/telegram.py` — sendMessage / sendPhoto через httpx, hashtags в конце текста
  - `app/publishers/vk.py` — wall.post v5.199 через httpx, URL vk.com/wall-{group_id}_{post_id}
  - `app/publishers/manual.py` — publish_manual() → manual_pending; mark_manual_done()
  - `app/services/scheduling.py` — schedule_content() (валидация + вставка); process_due() (тик с retry 3x + +5min)
  - `app/scheduler.py` — APScheduler BackgroundScheduler, job process_due каждую минуту
  - `app/main.py` — аддитивно: import start_scheduler/stop_scheduler, вызов в lifespan
  - `tests/test_publishers.py` — 7 тестов, все ✅
- **Результат:** `pytest tests/test_publishers.py -q` → **7 passed** ✅
- **Запуск завода:** из `A:\DevAI\Projects\KontentZavod`: `.venv\Scripts\uvicorn app.main:app --reload`
- **Запуск роутера:** из `A:\DevAI\Projects\LLMRouter`: `.venv\Scripts\python -m uvicorn app.main:app --port 8100`
- **Следующий шаг:** Объединить результат параллельного агента (pipeline, промпты, generate.py, test_pipeline.py) с текущей базой; затем — ревью-очередь и календарь в дашборде (волна 2.3 PLAN)

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

- Сервер: макс 4GB RAM / 2 vCPU / 30GB SSD → никаких Postgres/Redis/Celery/n8n/moviepy. SQLite + APScheduler + чистый ffmpeg.
- У пользователя RX 580 (AMD) — локальная генерация невозможна, всё через API.
- Платный бюджет: только DeepSeek API. Остальное — free-тиры и бесплатные сервисы.
- Браузер-автоматизация: отложена, только этап 6, только Gemini, только если пользователь захочет.
- Контент на русском. Несколько проектов: динамические, в БД, CRUD через дашборд (НЕ projects.yaml). Per-project креденшелы площадок — Fernet в project_platforms.
- Публикация: TG/VK авто; YouTube авто с оговоркой (audit); IG/Дзен — ручная очередь через дашборд.

- Автопереключение модели между сессиями: поле `"model"` в `.claude/settings.json` проекта (обновляет Claude в конце сессии). Сейчас стоит `sonnet` под этап 1.
- Stop-хук контроля свежести STATE.md заблокирован классификатором разрешений — ждёт решения пользователя (скрипт описан в журнале сессии 2026-06-10).

## Журнал сессий

- **2026-06-10 (1)** — проектирование системы, этап 0: PLAN/STATE/CLAUDE/agents. DeepSeek-ключ в `.env`, `.gitignore`, усилен протокол (обновление STATE/PLAN после каждого цикла).
- **2026-06-10 (2)** — решение пользователя: схема «оркестратор + сабагенты» вместо переключения модели главной сессии (поле model из settings.json убрано). Создан и протестирован Stop-хук свежести STATE.md (пропуск/блок/защита от цикла — все пути проверены; подхватится после перезапуска сессии или /hooks). **Этап 1: LLM-Router построен двумя sonnet-сабагентами и проверен оркестратором реальными вызовами DeepSeek.** Найден и исправлен баг: эндпоинт всегда отдавал SSE → теперь non-stream возвращает единый JSON, стрим не буферируется, токены пишутся. Веб-кабинет: логин, аккаунты (free выделены), ключи, статистика — все проверки прошли. Остался деплой на VPS.
