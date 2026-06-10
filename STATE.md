# STATE.md — состояние работы (обновляется каждой сессией)

## Текущая точка

- **Этап:** 0 ✅, 1 (LLM-Router) ✅, этап 2 ✅, этап 3 ✅, этап 4 ✅ (dry-run), **ЭТАП 5 / ВОЛНА 7 (аналитика и самообучение) ✅ на моках**. Этап 6 (Gemini-воркер) отложен по плану. **Реализация завода ЗАВЕРШЕНА** — дальше реальные ключи и прод.
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
- **Сделано (2026-06-10, волна 5 — видеоконвейер, оркестратор + 4 сабагента: 3 sonnet + 1 opus):**
  - Оркестратор заранее зафиксировал контракт: `config.py` (+FAKE_TTS, FAKE_ASSETS, PEXELS/PIXABAY_API_KEY, MEDIA_RETENTION_DAYS, FFMPEG/FFPROBE_BIN, тип 'int' в _FIELDS), `.env.example`, `requirements.txt` (+edge-tts==7.0.2, установлен в .venv)
  - `app/pipeline/tts.py` — synthesize_scenes(): per-сцена mp3 (edge-tts Dmitry/Svetlana), WordTiming/SceneAudio, тайминги в глобальной шкале, duration из ffprobe; FAKE_TTS=1 → тишина anullsrc + синтетические тайминги
  - `app/pipeline/subtitles.py` — build_ass(): 1080x1920, 2-3 слова на экран, karaoke {\k} подсветка текущего слова, MarginV=550
  - `app/pipeline/assets.py` — fetch_scene_assets(): футажи Pexels (portrait, height≥1280, не 4K) → фоллбэк Pixabay → фоллбэк картинка; слайдшоу Pollinations.ai (без ключа); pick_music(mood) из data/music/{mood}/; FAKE_ASSETS=1 → плейсхолдеры lavfi
  - `app/services/cleanup.py` — cleanup_after_render (удаляет media/{id}/assets/), rotate_media (ролики опубликованных старше MEDIA_RETENTION_DAYS + осиротевшие media-папки; files.purged=true); cron-джоб daily 04:00 UTC в scheduler.py
  - `app/pipeline/render.py` (opus) — render_video(): 5 проходов ffmpeg (сценные клипы → concat copy → голос AAC → финал с ducking sidechaincompress + вжигание .ass → превью jpg); mp4-футажи кроп/луп, jpg — Ken Burns zoompan с пред-апскейлом x2; хелпер escape_subtitles_path для Windows-путей
  - `app/prompts/video_script.txt` + `app/pipeline/video_script.py` — generate_video_script(idea_id, template): идея → JSON {video: {voice, mood, scenes[text, keywords(en)]}, посты-обвязка по площадкам (без dzen), features} → content(type=video_*, status='text_review'); заглушка purpose='video_script' в llm.py
  - `app/pipeline/produce.py` — produce_video(): tts → ass → assets → music → render (ленивый импорт _do_render) → cleanup → files(абс. пути) → status='review'; ошибки → files.produce_error + produce_attempts, статус остаётся production; process_production() — 1 контент/тик, attempts<3; джоб в scheduler.py (interval 1 мин, max_instances=1)
  - Веб: кнопки «🎬 Футажи»/«🎬 Слайдшоу» на идеях; ревью включает status='review'; видео в text_review — правка сцен + «В продакшн ▶»; в review — <video controls> + approve с планированием + «Перерендерить»; бейдж ошибки продакшна; `app/web/files.py` — GET /files/videos/{id}.mp4|.jpg
  - Тесты: test_tts.py (15), test_assets.py (16), test_render.py (9), test_video_flow.py (28)
- **Сделано (2026-06-10, волна 6 — публикация видео, оркестратор + 4 сабагента: 3 sonnet + 1 haiku):**
  - Оркестратор сам зафиксировал стыки ДО запуска агентов: `base.py` (+`PublishDeferred(retry_at)`, ветка youtube, files→vk), `scheduling.py` (process_due ловит PublishDeferred → перенос planned_at БЕЗ attempts+1)
  - `app/publishers/vk.py` — видео: video.save + wallpost=1 → multipart-аплоад → vk.com/video{owner}_{id}; клипы (shortVideo.create) недоступны обычным токенам — не делаем; без video_path — прежний wall.post
  - `app/publishers/telegram.py` — sendVideo (multipart, supports_streaming) при files.video_path, приоритет над sendPhoto
  - `app/publishers/youtube.py` (новый) — oauth refresh → resumable init → PUT, чистый httpx; URL youtube.com/shorts/{id}; title автодополняется "#Shorts"; квота YOUTUBE_DAILY_LIMIT=6 (config+.env.example): счётчик published за сегодня → PublishDeferred до полуночи UTC+5мин; 403 quotaExceeded → +12ч; privacy default private (до audit-верификации)
  - `app/web/manual.py` + `templates/manual/list.html` — ручная очередь: для IG текст из texts.instagram.caption (фоллбэк text); видео → плеер `<video>` с постером + «Скачать mp4» через /files/videos/{content_id}.mp4
  - Тесты: test_publishers_video.py (8), test_youtube.py (8), test_manual_video.py (3), e2e сценарий D в test_e2e.py (2: полный видео-цикл на 4 площадки + квота YT)
- **Сделано (2026-06-10, волна 7 — аналитика и самообучение, оркестратор + 4 сабагента: 3 sonnet + 1 opus):**
  - Оркестратор зафиксировал контракт сам: UNIQUE-индекс metrics(schedule_id, date) в db.py (идемпотентный INSERT OR REPLACE); джобы в scheduler.py добавил сам ПОСЛЕ агентов (collect_metrics daily 03:00 UTC, analyze_all weekly пн 05:00 UTC) — агентам scheduler.py был запрещён
  - `app/analytics/collector.py` — collect_metrics(): published за 30 дней (vk/youtube) → VK wall.getById / video.get, YouTube videos.list+oauth refresh → metrics; нет credentials → skip; ошибка одной публикации не валит сбор; dry-run URL пропускаются; Telethon для TG отложен (докстринг)
  - `app/web/analytics.py` + `templates/analytics/index.html` — /analytics: карточки, Chart.js (line по дням с datasets по платформам, bar средних), топ-5/анти-топ-5 с бейджами features, фильтры project/days, ручной ввод метрик (INSERT OR REPLACE, предзаполнение); пункт «Аналитика» в base.html; include_router в main.py
  - `app/analytics/analyzer.py` (opus) + `app/prompts/analyze.txt` — analyze_project/analyze_all: последний замер на публикацию + features → LLM (purpose='analyze', json_mode, 1 retry) → learnings source='analyzer' (weight clamp 0.5–2.0, дедуп по тексту, ≤5 за прогон, ≤10 активных/проект — лишние с меньшим weight деактивируются), deactivate только analyzer-инсайтов; <5 публикаций → skip без LLM; заглушка FAKE_LLM purpose='analyze'
  - A/B хуков: `app/prompts/script_ab.txt` + `script.py::generate_script_ab(idea_id)→(id_a,id_b)` — один вызов LLM → variants A/B (полные тексты площадок, разные hook_type) → 2 content c features.ab_variant + суффиксы [A]/[B] в title; кнопка «A/B пост» в _ideas_list.html + роут в generate.py; заглушка purpose='script_ab'
  - Тесты: test_collector.py (5), test_analytics.py (29), test_analyzer.py (7), test_ab.py (15)
- **Результат (волна 7):** `pytest tests -q` → **197 passed** ✅; смоук оркестратора: 7 URL (включая /analytics) → 200, при ENABLE_SCHEDULER=1 зарегистрированы все 5 джобов (analyze_all, collect_metrics, process_due, process_production, rotate_media) ✅; продовая data/ чистая ✅
- **Результат (волна 6):** `pytest tests -q` → **153 passed** ✅; смоук оркестратора `/` `/review` `/calendar` `/manual` `/projects` → 200, `/files/videos/999.mp4` → 404 ✅; продовая data/ чистая ✅
- **Результат (волна 5):** `pytest tests -q` → **132 passed** ✅; сквозная интеграция оркестратора (без моков рендера, настоящий ffmpeg): оба шаблона → валидный 1080x1920 mp4 22.4с, assets подчищены, статус review ✅; смоук `/` `/review` `/calendar` `/manual` `/projects` → 200, `/files/videos/999.mp4` → 404 ✅; продовая data/ чистая
- **Запуск завода:** из `A:\DevAI\Projects\KontentZavod`: `.venv\Scripts\uvicorn app.main:app --reload`
- **Запуск роутера:** из `A:\DevAI\Projects\LLMRouter`: `.venv\Scripts\python -m uvicorn app.main:app --port 8100`
- **Следующий шаг:** Этап 5 — аналитика и самообучение (`analytics/collector.py` ежедневный сбор метрик, дашборд аналитики, `analytics/analyzer.py` еженедельные learnings, A/B хуков). Реальные ключи пользователь пока не даёт — collector делать с FAKE/dry-run-режимом + форма ручного ввода метрик (она и без ключей рабочая). По-прежнему висит: глазами проверить реальный ролик (PEXELS/PIXABAY ключи) — перед продом.

## Что нужно от пользователя (блокеры)

- ~~DeepSeek-ключ~~ ✅ лежит в `.env` (DEEPSEEK_API_KEY) и добавлен в роутер. Пользователь обещал перевыпустить ключ (он засветился в чате) — при 401 спросить новый.
- Free-ключи (Gemini aistudio.google.com / Groq console.groq.com / OpenRouter) — пользователь может добавлять сам через кабинет http://127.0.0.1:8100/admin (пароль changeme — сменить в .env роутера!).
- Данные своих проектов пользователь введёт САМ через дашборд `/projects` (профиль: описание, ЦА, тон, цели, CTA, темы).
- Деплой роутера: пользователь выполняет сам по `LLMRouter\DEPLOY.md` (после деплоя: сменить ADMIN_PASSWORD, перевыпустить виртуальные ключи).
- Токены TG-бота / VK-сообщества — ОТЛОЖЕНО по решению пользователя (подтверждено 2026-06-10, волна 6: «ключи не дам, нет времени»): всё проверяем e2e без реальных API (FAKE_LLM + dry-run паблишеры), реальные токены — после полной реализации.
- YouTube: пользователю нужно будет создать OAuth-приложение в Google Cloud Console (client_id/client_secret/refresh_token в credentials площадки) и подать заявку на audit-верификацию (до одобрения видео лочатся в private — поэтому default privacy='private'). VK-токен сообщества должен иметь права video+wall.

## Нюансы и грабли (накапливаем)

- **Публикация видео (волна 6):** `PublishDeferred(retry_at)` из base.py — НЕ ошибка: process_due переносит planned_at и НЕ увеличивает attempts (для дневных квот). Квота YouTube считается по `schedule: platform='youtube' AND status='published' AND date(updated_at)=date('now')` — проверяется и в dry-run (чистая работа с БД). В тестах process_due патчить `app.services.scheduling.publish` (from-import при загрузке модуля), не `app.publishers.base.publish`. PS 5.1: `python -c "..."` через переменную теряет внутренние кавычки — однострочные питон-проверки писать во временный .py-файл.
- **Видеоконвейер (волна 5):** ffmpeg/ffprobe локально — `C:\FFmpeg\bin` (в PATH); на сервере — `apt install ffmpeg` (добавить в DEPLOY.md на этапе 4). `subtitles=`-фильтр на Windows требует экранирования пути (`C\:/...`) — есть хелпер `render.escape_subtitles_path`. concat demuxer с `-c copy` требует строго одинаковых параметров сценных клипов (один пресет + setsar=1). zoompan дрожит на маленьком входе — лечится пред-апскейлом x2. Karaoke ASS: пауза между словами добавляется к `\k` СЛЕДУЮЩЕГО слова.
- TestClient БЕЗ контекст-менеджера не запускает lifespan (init_db) → «no such table». Смоук только через `with TestClient(app) as c:`.
- В тестах рендер мокается через `app.pipeline.produce._do_render` (ленивый импорт render_video внутри produce — НЕ импортировать на уровне модуля).
- Pixabay Videos API не умеет orientation — берём medium/small, рендер кадрирует. Pollinations.ai: GET image.pollinations.ai/prompt/{prompt}?width=1080&height=1920&nologo=true, без ключа, промпт на английском.

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

- **2026-06-10 (5)** — **ЭТАП 4 (ПУБЛИКАЦИЯ ВИДЕО) ЗАВЕРШЁН в dry-run, ВОЛНА 6** (оркестратор + 4 сабагента: VK/TG-видео [S] ∥ YouTube [S] ∥ manual-UI [H], затем e2e-сценарий D [S]). Оркестратор заранее зафиксировал стыки сам: base.py (PublishDeferred, ветка youtube, files→vk) и scheduling.py (перенос без сжигания attempts). Итог: TG sendVideo, VK video.save+wallpost (клипы недоступны — партнёрский метод), YouTube Shorts через чистый httpx с квотой 6/день и переносом по PublishDeferred, IG в ручной очереди с плеером и «Скачать mp4». **pytest 153/153 ✅, смоук 5 страниц 200 ✅, data/ чистая.** Реальные ключи пользователь не дал (нет времени) — приёмка с живыми токенами отложена. Дальше — этап 5 (аналитика, collector с dry-run + ручной ввод метрик).
- **2026-06-10 (1)** — проектирование системы, этап 0: PLAN/STATE/CLAUDE/agents. DeepSeek-ключ в `.env`, `.gitignore`, усилен протокол (обновление STATE/PLAN после каждого цикла).
- **2026-06-10 (2)** — решение пользователя: схема «оркестратор + сабагенты» вместо переключения модели главной сессии (поле model из settings.json убрано). Создан и протестирован Stop-хук свежести STATE.md (пропуск/блок/защита от цикла — все пути проверены; подхватится после перезапуска сессии или /hooks). **Этап 1: LLM-Router построен двумя sonnet-сабагентами и проверен оркестратором реальными вызовами DeepSeek.** Найден и исправлен баг: эндпоинт всегда отдавал SSE → теперь non-stream возвращает единый JSON, стрим не буферируется, токены пишутся. Веб-кабинет: логин, аккаунты (free выделены), ключи, статистика — все проверки прошли. Остался деплой на VPS.
- **2026-06-10 (4)** — **ЭТАП 3 (ВИДЕОКОНВЕЙЕР) ЗАВЕРШЁН, ВОЛНА 5** (оркестратор + 4 сабагента: tts/субтитры [S] ∥ ассеты/ротация [S], затем рендер [O] ∥ склейка+веб [S]). Оркестратор заранее развёл конфликты: config/.env.example/requirements правил сам до запуска параллельных агентов; стык produce↔render через ленивый импорт. Итог: полный конвейер идея → видео-сценарий → TTS → субтитры → ассеты → рендер → ревью с превью. pytest 132/132 ✅, сквозная интеграция с настоящим ffmpeg ✅ (оба шаблона, 1080x1920). Дальше — этап 4 (публикация видео), перед ним глазами проверить реальный ролик (нужны бесплатные PEXELS/PIXABAY ключи).
- **2026-06-10 (3)** — **ЭТАП 2 ПОЛНОСТЬЮ ЗАВЕРШЁН** (оркестратор + 6 sonnet-сабагентов, 4 волны). (а) `LLMRouter\DEPLOY.md` — копипаст-деплой роутера (systemd + поддомен в существующий Caddyfile + обновление rsync). (б) Перепроектирование по требованию пользователя: проекты динамические в БД с CRUD через дашборд, projects.yaml отменён, per-project креденшелы Fernet. (в) Волна 1: скелет, 7 таблиц, CRUD проектов/площадок, llm.py+FAKE_LLM. (г) Волна 2 (2 параллельных агента): pipeline ideas/script + промпты ideas.txt/script.txt; publishers TG/VK/manual с dry-run в data/outbox + scheduler. Интеграционный фикс стыка волн: Settings → динамическое чтение env (5 падений устранены в корне). (д) Волна 3: ревью-очередь с правкой и approve+планированием, reject→learnings, календарь месяц/неделя с переносом/retry, ручная очередь, сводка на главной. (е) Волна 4: e2e A/B/C (полный цикл по HTTP, петля обучения reject→промпт, отказоустойчивость 3 ретрая→error→retry) + `DEPLOY.md` завода (порт 8200, basic_auth через Caddy — у дашборда нет своего логина!). Финальная верификация оркестратора: **pytest 64/64 ✅, смоук 5 страниц 200 ✅, продовые data/ чистые**. Нюанс волны 4: `from app.llm import chat` в ideas.py → в тестах патчить `app.pipeline.ideas.chat`, не `app.llm.chat`.
