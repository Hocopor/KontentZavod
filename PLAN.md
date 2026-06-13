# PLAN.md — живой план реализации «КонтентЗавод»

> **Это живой документ.** Чекбоксы обновляются по ходу работы. Текущая точка и нюансы — в `STATE.md`.
> У каждой задачи указана модель-исполнитель: `[H]` haiku (простое), `[S]` sonnet (основное), `[O]` opus (сложное/архитектура).

## Обзор системы

```
                    ┌─────────────────────────────┐
                    │  LLM-Router (отдельный проект│
  DeepSeek ──────►  │  A:\DevAI\Projects\LLMRouter)│
  Gemini free ───►  │  пул ключей/аккаунтов,       │
  Groq free ─────►  │  1 универсальный ключ,       │
  OpenRouter ────►  │  приоритет бесплатным,       │
  Mistral free ──►  │  фоллбэки, кабинет, статистика│
                    └──────────────┬──────────────┘
                                   │ OpenAI-совместимый API
                    ┌──────────────▼──────────────┐
                    │      КонтентЗавод (4GB VPS) │
                    │ идеи → сценарий → ревью →   │
                    │ озвучка/футажи/рендер →     │
                    │ ревью → календарь →         │
                    │ публикация → метрики →      │
                    │ анализ → learnings → в промпты│
                    └──────────────┬──────────────┘
            ┌──────────┬──────────┼──────────┬──────────┐
         Telegram     VK      YouTube    Instagram    Дзен
         (авто)     (авто)  (авто/private⚠️) (ручная)  (ручная)
```

Бюджет: 0₽ + DeepSeek API (копейки). Железо: VPS 4GB RAM / 2 vCPU / 30GB SSD.

---

## Этап 0 — инфраструктура разработки ✅

- [x] `[S]` PLAN.md (этот файл), STATE.md, CLAUDE.md
- [x] `[S]` `.claude/agents/`: coder-simple (haiku), coder (sonnet), architect (opus)

**Приёмка:** новая сессия + «продолжай» → Claude по STATE.md называет точку и рекомендует модель, не сканируя проект.

---

## Этап 1 — LLM-Router (отдельный проект `A:\DevAI\Projects\LLMRouter`)

Самостоятельный продукт. Стек: FastAPI + SQLite + Jinja2/HTMX. Один процесс, влезает в 4GB вместе с заводом.

### 1.1 Скелет и БД
- [x] `[H]` Структура проекта, requirements.txt, .env.example, запуск uvicorn
- [x] `[S]` Схема SQLite:
  - `providers` (type: deepseek/gemini/groq/openrouter/mistral, base_url)
  - `accounts` (provider_id, name, api_key — зашифрован Fernet, tier: free/paid, enabled)
  - `account_limits` (account_id, period: rpm/rpd/tpm, limit, счётчик, reset_at)
  - `virtual_keys` (key_hash, name, enabled, created_at)
  - `usage_log` (ts, vkey_id, provider, account, model, tokens_in, tokens_out, latency_ms, status)
- [x] `[H]` CLAUDE.md роутера (ссылка на этот PLAN/STATE)

### 1.2 Ядро роутинга
- [x] `[S]` Адаптеры провайдеров: `adapters/openai_compat.py` (один универсальный для deepseek/groq/openrouter/mistral — отличаются base_url) + `adapters/gemini.py` (конвертация формата). Новый провайдер = строка в catalog.py
- [x] `[S]` Маршрутизатор: разбор `model` (`auto` | `provider/model` | `provider@account/model`), выбор аккаунта (free first → LRU → paid), учёт лимитов rpm/rpd, на 429/5xx/таймаут — автофоллбэк, макс 3 попытки
- [x] `[S]` `POST /v1/chat/completions` (non-stream JSON + SSE streaming), `GET /v1/models`, `GET /health`, Bearer-аутентификация по виртуальному ключу; токены пишутся в usage_log
- [x] `[S]` Дефолтные лимиты free-тиров в catalog.py; CLI `app/manage.py` (add-account, gen-key, list, revoke)

### 1.3 Личный кабинет
- [x] `[S]` Логин (пароль в .env, HMAC-подписанная cookie, TTL 7 дней)
- [x] `[H]` CRUD аккаунтов: добавление (ключ шифруется Fernet), toggle, удаление, правка лимитов; **free-аккаунты выделены зелёным** и показаны первыми, прогресс-бары лимитов
- [x] `[H]` Выпуск/отзыв виртуальных ключей (полный ключ показывается один раз)
- [x] `[S]` Статистика: дашборд (сегодня), график по дням (Chart.js), разбивки по провайдерам/аккаунтам, фильтр 7/14/30 дней
- [x] `[S]` Копипаст-инструкция деплоя `LLMRouter\DEPLOY.md`: первичный деплой (/opt/llmrouter, venv, .env), systemd unit (127.0.0.1:8100, MemoryMax=512M, user llmrouter), блок поддомена в существующий Caddyfile, процедура обновления (rsync --exclude data/.env/.venv), диагностика. Заливает на сервер сам пользователь
- [ ] `[H]` Фактический деплой на VPS — выполняет пользователь по DEPLOY.md (после: сменить ADMIN_PASSWORD, перевыпустить виртуальные ключи)

**Приёмка:** `curl` с универсальным ключом, `model: auto` → ответ от бесплатного провайдера; симулировать 429 → автофоллбэк; кабинет показывает расход; ключ отозван → 401.

---

## Этап 2 — КонтентЗавод: ядро + текстовый контент

После этого этапа завод уже работает: постит в Telegram и VK.

### 2.1 Скелет
- [x] `[H]` Структура (см. CLAUDE.md), requirements.txt, .env.example, uvicorn — **ВОЛНА 1 ✅**
- [x] `[S]` Схема SQLite (все 7 таблиц: projects, project_platforms, ideas, content, schedule, metrics, learnings) — **ВОЛНА 1 ✅**
- [x] `[S]` CRUD проектов + площадок с HTMX UI (список, форма, детальная, Fernet-токены) — **ВОЛНА 1 ✅** (заменяет projects.yaml — динамические проекты через дашборд)
- [x] `[H]` `app/llm.py` — клиент LLM-Router (openai SDK с base_url роутера), FAKE_LLM для тестов — **ВОЛНА 1 ✅**

### 2.2 Генерация текста
- [x] `[S]` `pipeline/ideas.py`: идеи по проекту (промпт включает описание проекта + активные learnings + список прошлых тем против повторов) — **ВОЛНА 2 ✅**
- [x] `[S]` `pipeline/script.py`: из идеи → пост per-платформа (хук, тело, CTA, хэштеги; лимиты длины VK/TG/IG/Дзен) + заполнение `features` — **ВОЛНА 2 ✅**
- [x] `[S]` Промпты в `app/prompts/` (ideas.txt, script.txt, плейсхолдеры <<VAR>>) + `pipeline/prompts.py` загрузчик — **ВОЛНА 2 ✅**
- [x] `[S]` Веб: секция «Идеи» на детальной странице проекта (генерация HTMX, ручное добавление, «Сделать пост», «Отклонить») — **ВОЛНА 2 ✅**
- [x] `[S]` Интеграционный фикс после параллельных волн: Settings читает env динамически (lookup overrides → os.environ → default), единый `_build_detail_context`, 38/38 тестов ✅

### 2.3 Дашборд: ревью и календарь
- [x] `[S]` Очередь ревью: список черновиков, просмотр, inline-правка текста, approve / reject(причина → learnings) — **ВОЛНА 3 ✅**
- [x] `[S]` Календарь (месяц/неделя): что, где, когда; цвет = статус (план/опубликовано/ошибка/вручную/авто); перенос даты — **ВОЛНА 3 ✅**
- [x] `[H]` Главная: сводка (в очереди, запланировано, ошибки) — **ВОЛНА 3 ✅**
- [x] `[S]` Ручная очередь `/manual`: manual_pending → копировать текст + URL → manual_done — **ВОЛНА 3 ✅**

### 2.4 Публикация текста
- [x] `[S]` Единый интерфейс `publishers/base.py` (publish(schedule_id) → url|error, dry-run в data/outbox/)
- [x] `[S]` `publishers/telegram.py` — Bot API sendMessage/sendPhoto через httpx, hashtags в конце
- [x] `[S]` `publishers/vk.py` — wall.post v5.199 через httpx, URL vk.com/wall-{group_id}_{post_id}
- [x] `[S]` `publishers/manual.py` — publish_manual() → manual_pending; mark_manual_done(); UI ручной очереди — волна 3
- [x] `[S]` `services/scheduling.py` — schedule_content(), process_due() (тик: retry 3x, +5 min, error)
- [x] `[S]` `scheduler.py` (APScheduler): BackgroundScheduler, job process_due каждую минуту, ENABLE_SCHEDULER=0 по умолчанию

- [x] `[S]` e2e-тест `tests/test_e2e.py` (3 сценария: A — полный lifecycle через HTTP в dry-run, B — reject-петля обучения, C — отказоустойчивость retry/error/retry-роут) — **ВОЛНА 4 ✅, dry-run**
- [x] `[H]` `DEPLOY.md` завода (согласован с LLMRouter/DEPLOY.md: /opt/kontentzavod, порт 8200, basic_auth Caddy, FERNET_KEY предупреждение, MemoryMax с примечанием ffmpeg) — **ВОЛНА 4 ✅**

**Приёмка:** сгенерить пост → одобрить → автопост в тестовый TG-канал и VK-группу, статус обновился; сломанный токен → статус «ошибка» с текстом; Дзен-пост прошёл ручную очередь. ✅ **e2e пройдена в dry-run (FAKE_LLM=1, PUBLISH_DRY_RUN=1), 64/64 тестов зелёные.**

---

## Этап 3 — видеоконвейер

### 3.1 Озвучка и субтитры
- [x] `[S]` `pipeline/tts.py`: edge-tts (ru-RU-DmitryNeural / SvetlanaNeural), сохранение WordBoundary-таймингов — **ВОЛНА 5 ✅** (per-сцена mp3 + глобальные тайминги, FAKE_TTS для тестов)
- [x] `[S]` `pipeline/subtitles.py`: тайминги → .ass (крупный стиль для вертикали, 2-3 слова на экран, подсветка текущего слова) — **ВОЛНА 5 ✅** (karaoke \k)

### 3.2 Ассеты
- [x] `[S]` `pipeline/assets.py`: ключевые слова сцен (англ., даёт LLM в video_script) → футажи Pexels → фоллбэк Pixabay → фоллбэк картинка; картинки Pollinations.ai (Gemini через роутер отложен) — **ВОЛНА 5 ✅** (FAKE_ASSETS для тестов)
- [x] `[H]` Локальная музыкальная библиотека `data/music/{mood}/` + случайный выбор по настроению — **ВОЛНА 5 ✅** (пусто → рендер без музыки)
- [x] `[H]` Ротация диска: `services/cleanup.py` — исходники после рендера, финальные ролики через MEDIA_RETENTION_DAYS после публикации; cron-джоб daily 04:00 UTC — **ВОЛНА 5 ✅**

### 3.3 Рендер (самое хитрое место)
- [x] `[O]` `pipeline/render.py` — чистый ffmpeg, 1080x1920 — **ВОЛНА 5 ✅**:
  - шаблон А «футажи+саб»: нарезка по длительности фраз, кроп в вертикаль, озвучка + музыка (sidechaincompress ducking), вжигание .ass
  - шаблон Б «слайдшоу»: Ken Burns (zoompan с пред-апскейлом x2), музыка; тип сцены определяется расширением ассета — миксы mp4+jpg работают
  - RAM-стратегия: 5 проходов (сценные клипы → concat copy → голос → финал → превью), НЕ один гигантский filter_complex; очередь — process_production по 1 контенту/тик, max_instances=1
- [x] `[S]` Видео-превью в ревью-очереди дашборда (`<video controls>` + постер, раздача `/files/videos/`) — **ВОЛНА 5 ✅**
- [x] `[S]` Склейка конвейера (сверх плана): `prompts/video_script.txt` + `pipeline/video_script.py` (идея → сцены+посты-обвязка), `pipeline/produce.py` (tts→ass→assets→music→render→review; ошибки в files.produce_error, ≤3 попыток), кнопки «🎬 Футажи»/«🎬 Слайдшоу», флоу видео в ревью (text_review→production→review→approved), перерендер — **ВОЛНА 5 ✅**

**Приёмка:** тестовый сценарий → валидный 1080x1920 mp4 ≤60с, оба шаблона работают, рендер не валит сервер по памяти. ✅ **Сквозная интеграция пройдена (FAKE_LLM/TTS/ASSETS + настоящий ffmpeg): оба шаблона → 1080x1920 mp4 22.4с, исходники подчищены, 132/132 тестов.** Проверка субтитров глазами — на реальном контенте после ввода данных проекта.

---

## Этап 4 — публикация видео

- [x] `[S]` `publishers/vk.py`: video.save с wallpost=1 (видео в стену) — **ВОЛНА 6 ✅**; `shortVideo.create` исследован: партнёрский/закрытый метод, обычным токенам сообществ недоступен → клипы не делаем, видео в стену
- [x] `[S]` `publishers/youtube.py`: OAuth refresh-token + resumable upload через Data API v3 (httpx, без google-client) — **ВОЛНА 6 ✅**; квота YOUTUBE_DAILY_LIMIT=6/день: превышение → `PublishDeferred` (перенос planned_at БЕЗ сжигания attempts, ловится в process_due); 403 quotaExceeded от API → перенос +12ч; privacy default 'private' (до audit). Заявку на audit-верификацию подаёт пользователь в Google Cloud Console
- [x] `[S]` `publishers/telegram.py`: sendVideo для видео-контента (сверх плана) — **ВОЛНА 6 ✅**
- [x] `[H]` Instagram через ручную очередь: /manual показывает плеер + «Скачать mp4» + caption (для IG берётся texts.instagram.caption) — **ВОЛНА 6 ✅**
- [x] `[S]` e2e сценарий D в `tests/test_e2e.py`: полный видео-цикл идея→сценарий→продакшн→approve→публикация на 4 площадки (dry-run) + квота YouTube — **ВОЛНА 6 ✅**
- [ ] `[S]` (позже, опционально) Instagram Graph API при наличии Business-аккаунта

**Приёмка:** одобренный ролик автоматом улетел в VK и YouTube (или корректно лёг в ручную очередь), статусы в календаре верные. ✅ **Пройдена в dry-run (e2e сценарий D): TG sendVideo + VK video.save + YT videos.insert в outbox, IG через /manual с плеером; 153/153 тестов. С реальными токенами — после их получения.**

---

## Этап 5 — аналитика и самообучение

- [x] `[S]` `analytics/collector.py` (ежедневно 03:00 UTC): VK wall.getById/video.get, YouTube videos.list statistics (oauth refresh); нет credentials → skip; INSERT OR REPLACE по UNIQUE(schedule_id, date) — **ВОЛНА 7 ✅**. TG-просмотры через Telethon ОТЛОЖЕНЫ до реальных ключей (api_id/api_hash + новая зависимость) — пока TG вручную
- [x] `[H]` Дашборд аналитики `/analytics`: Chart.js (динамика просмотров по дням/платформам, bar средних по платформам), сводные карточки, топ-5/анти-топ-5, фильтры проект/период (7/30/90) + форма ручного ввода метрик для любых публикаций (IG/Дзен/TG) — **ВОЛНА 7 ✅**
- [x] `[O]` `analytics/analyzer.py` (еженедельно пн 05:00 UTC): LLM (promts/analyze.txt, purpose='analyze') коррелирует features с метриками → INSERT в learnings (source='analyzer', weight 0.5–2.0, дедуп, ≤5 за прогон, ≤10 активных/проект) + deactivate опровергшихся (reject-learnings не трогает); <5 публикаций с метриками → skip без LLM — **ВОЛНА 7 ✅**
- [x] `[S]` A/B: кнопка «A/B пост» → `generate_script_ab` (промпт script_ab.txt) → 2 контента с разными хуками, features.ab_variant='A'/'B', разные hook_type; analyzer сравнивает пары — **ВОЛНА 7 ✅**

**Приёмка:** после ≥10 публикаций analyzer выдаёт осмысленные learnings; они видны в промптах генерации; в дашборде понятно, что заходит и почему. ✅ **Механика проверена на FAKE_LLM/моках: 197/197 тестов, цепочка metrics → analyzer → learnings → промпты ideas/script работает; осмысленность learnings — после реальных публикаций.**

---

## Этап 6 — (ОТЛОЖЕНО, по желанию) Gemini-воркер на домашнем ПК

- [ ] `[O]` Playwright-автоматизация Gemini (Veo) на десктопе: воркер забирает задания с сервера по HTTP, генерит видео в браузере под своим аккаунтом, заливает результат. ⚠️ Нарушение ToS, риск бана аккаунта — только осознанно.

---

## Этап 7 — Завод 2.0: автономный цикл «проект → стратегия → план → фабрика»

Редизайн воркфлоу (запрос пользователя 2026-06-10, после деплоя). Целевой флоу:
создать проект (имя + описание + цель, остальное придумывает ИИ, всё редактируемо) →
подключить площадки (актуализировать подключения) → настройки запуска (платформы, типы контента
per-платформа, горизонт контент-плана, lookahead генерации, retention) → кнопка «▶ В работу» →
ИИ-маркетинговая стратегия per-платформа → контент-планы per-платформа/per-тип (rolling, всегда
покрытие на горизонт) → шахматка согласования (строки: платформа→тип, столбцы: дни; модалка с
правкой; одобрение поштучно/всё) → «Генерация одобренного» (один раз нажал — дальше rolling, по
1 за тик, на lookahead дней вперёд, без регенерации готового) → вторая шахматка готового контента
(превью, скачивание) → автопубликация по дате/времени плана (manual-типы → ручная очередь) →
аналитика → пересмотр стратегии по метрикам + комментарий пользователя. Паузы плана и генерации —
раздельные.

### Контракты этапа 7 (зафиксированы оркестратором — менять только через PLAN)

- **`app/catalog.py`** — каталог типов контента per платформа (только корректные для платформы):
  `telegram: post, video` · `vk: post, video, story(manual)` · `instagram: post, story, reel — все manual` ·
  `youtube: short` · `dzen: post, article`. Атрибуты типа: `label` (рус.), `kind` (`text|video|story`),
  `publish` (`auto|manual`), `default_on` (bool). Хелперы: `allowed_types(platform)`, `default_types(platform)`, `type_info(platform, ctype)`.
- **БД** (миграции в db.py, идемпотентно): новые таблицы
  `strategies(id, project_id, version, status: generating|active|archived|error, strategy JSON, inputs JSON, user_comment, error_text, created_at, UNIQUE(project_id,version))`;
  `plan_items(id, project_id, strategy_id, platform, content_type, date YYYY-MM-DD, time_slot HH:MM, title, brief JSON, status: proposed|approved|rejected|generating|generated|error, content_id→content, error_text, created_at, updated_at)`.
  ALTER: `projects + stage (draft|running|paused, default draft)`, `projects + settings JSON`
  (`plan_horizon_days=30, gen_lookahead_days=3, retention_days=14, plan_paused=0, gen_paused=0, autogen=0`);
  `project_platforms + content_types JSON` ({"post": true, …}, NULL = default_on из каталога).
  Пересборка `content`: CHECK type расширен до `('post','story','article','video_footage','video_slideshow')`.
- **llm.py** — новые purposes + FAKE-заглушки: `profile` (поля projects: audience/tone/cta/themes/forbidden/extra),
  `strategy` и `strategy_revise` ({summary, positioning, platforms: {<platform>: {goals, rubrics[], content_mix {<type>: N в неделю}, best_times[], kpi}}, у revise + changes_summary),
  `plan` ({items: [{date, time, content_type, title, brief {hook, outline, cta, keywords[]}}]}),
  `item_post` (как script, но одна платформа), `item_story` ({title, image_prompt(en), overlay_text, caption, features}).
- **Джобы** (scheduler.py агентам ЗАПРЕЩЁН, подключает оркестратор): `process_brain` (2 мин):
  стратегии generating → build_strategy; для running-проектов покрытие плана < горизонта и не plan_paused →
  догенерация плана по 1 платформе за тик. `process_factory` (1 мин): 1 approved plan_item с
  date ≤ today+lookahead, content_id IS NULL, autogen=1, не gen_paused → генерация контента по kind +
  авто-schedule на дату/время плана (manual-типы → сразу manual_pending).
- **Веб-стыки**: роутеры-заглушки web/launch.py, web/strategy.py, web/plan.py, web/queue.py созданы
  оркестратором и подключены в main.py ДО волн — агенты их наполняют, main.py не трогают.
  Кнопка «Стратегия» из _launch.html ведёт на GET `/projects/{slug}/strategy` (реализует web/strategy.py).

### 7.1 Фундамент (ВОЛНА A)
- [x] `[S]` db.py: таблицы strategies/plan_items + миграции (ALTER projects/project_platforms, пересборка content) + catalog.py + заглушки llm.py (6 purposes) + тесты — **ВОЛНА A ✅ 247/247**
### 7.2 Проект и стратегия (ВОЛНА B, параллельно) — **✅ 310/310**
- [x] `[S]` prompts/profile.txt + pipeline/profile.py + двухшаговое создание проекта (шаг 1: имя/описание/цель → «Придумать остальное»; шаг 2: редактируемый ИИ-профиль)
- [x] `[S]` web/launch.py + projects/_launch.html: настройки запуска (типы контента по каталогу, горизонты, retention), «▶ В работу» (stage=running + strategies(generating)), паузы plan/gen, стоп — **ВОЛНА B ✅ 298/298**
- [x] `[O]` pipeline/strategy.py: build_strategy(strategy_id) (мультишаговый: inputs → аналитическая записка → стратегия/revise-ветка при user_comment → фильтрация по каталогу → active, прошлые archived; ошибки → status=error) + prompts/strategy*.txt + web/strategy.py (просмотр/правка/пересмотр/retry)
### 7.3 План и фабрика (ВОЛНА C, параллельно) — **✅ 388/388**
- [x] `[S]` pipeline/planner.py + prompts/plan.txt (план по стратегии на период, per-платформа, только включённые типы) + services/brain.py (process_brain: build стратегий по 1 за тик + rolling-покрытие плана по 1 платформе за тик) — **ВОЛНА C ✅ 332/332**
- [x] `[O]` pipeline/from_plan.py (text→item_post per-платформенный texts, video→idea+video_script сразу в production, story→Pollinations-картинка+caption) + services/factory.py (process_factory: 1 пункт за тик, дожим готовых видео в schedule, авто-schedule на дату/время плана, manual-типы → manual_pending, без регенерации)
- [x] `[S]` web/plan.py — шахматка согласования: строки платформа→тип (details open), столбцы — дни месяца (переключение месяц/год, sticky первый столбец), модалка (правка title/даты/брифа, одобрить/отклонить), «Одобрить всё за месяц», toggle «⚡ Генерация одобренного» (autogen), индикаторы пауз; «🗓 План» в base.html
### 7.4 Очередь готового и наладка (ВОЛНА D) — СЛЕДУЮЩАЯ
- [x] `[S]` web/queue.py — шахматка готового контента (превью пост/видео/картинка, модалка, скачивание файлов, статус публикации, отмена/retry) + пункт «📦 Очередь» в base.html — **ВОЛНА D ✅** (роут /files/images/{id}.jpg добавил оркестратор до волны)
- [x] `[S]` актуализация подключения площадок (именованные формы credentials с подсказками per-платформа + POST /check c httpx-проверкой TG getMe / VK groups.getById / YT oauth refresh), retention_days per project в cleanup (MEDIA_RETENTION_DAYS — фоллбэк), manual-очередь для story (картинка+caption+скачать), аналитика с разрезом по типам контента — **ВОЛНА D ✅ 459/459**
- [x] `[S]` e2e-сценарий E: полный новый флоу (проект из 3 полей → профиль → launch → brain → стратегия → план → approve → factory → schedule → publish dry-run → /queue published + проверка пауз brain/factory) + DEPLOY.md дополнен (миграции этапа 7, джобы brain/factory, абзац «Воркфлоу 2.0») — **ВОЛНА D ✅ 460/460**
- [x] оркестратор: джобы process_brain (2 мин) + process_factory (1 мин) в scheduler.py, смоук 8 страниц 200 + 7 джобов ✅, полный pytest 388/388 ✅

**Приёмка:** создать проект из 3 полей → ИИ-профиль редактируем → «В работу» → стратегия появилась и
читабельна → контент-план в шахматке по включённым типам → одобрить часть → «Генерация одобренного» →
контент создан только для одобренных в lookahead-окне, schedule на дату плана, manual-типы в ручной
очереди → /queue показывает готовое с превью и скачиванием → паузы останавливают brain/factory →
пересмотр стратегии создаёт v2 с учётом комментария. Всё на FAKE_LLM + dry-run, полный pytest зелёный.
✅ **Пройдена e2e-сценарием E (FAKE_LLM + dry-run): 460/460 тестов, смоук 9 страниц → 200, 7 джобов.
Этап 7 реализован; с реальными ключами/LLM — после деплоя на mako-play.**

## Этап 7.5 — UX-редизайн по приёмке пользователя (2026-06-11) ✅

Жалобы приёмки: профиль ИИ «не в тему», дубли полей формы, мешанина старого и нового флоу,
непонятные кнопки, контент «улетает в никуда», запланированное не публиковалось вовремя.

- [x] `[оркестратор]` Корень «не в тему»: имена полей шага 1 ≠ параметры Form роута generate-profile → LLM получал пустые данные. Фикс имён + замена шага 1 целиком (нет дублей полей) + чистка дублей в .env
- [x] `[S]` Навигация: две рабочие вкладки «Контент-план» (/plan) и «Публикация» (/queue) с бейджами; часы сервера в шапке; снос /review, /calendar, /manual, /generate (+шаблоны и веб-тесты); дашборд под новый флоу + баннер при ENABLE_SCHEDULER=0; CSS-контракт `.board*`/`.chip-*`/`.dot-*`/`.legend`
- [x] `[S]` «Публикация»: шахматка готового, модалка-превью, «Перегенерировать»/«Удалить»/«Отменить публикацию», ручная публикация на той же странице, секция «Вне плана» (осиротевший контент + удаление); фикс временной базы process_due (UTC → локальное время сервера)
- [x] `[S]` «Контент-план»: «⚡ Генерация одобренного: ВКЛ/ВЫКЛ» с пояснением, чипы статусов с легендой, «Удалить пункт» (с контентом — 422 и отсылка в Публикацию), «Одобрить все предложенные за месяц (N)», empty-state
- [x] `[S]` Страница проекта: секция «Идеи» удалена, структура «1. Профиль → 2. Площадки → 3. Запуск», stage-чип, кнопки перехода в обе вкладки после запуска

**Приёмка:** pytest 459/459 ✅, смоук: /, /projects, /projects/new, /plan, /queue, /analytics → 200;
/review, /calendar, /manual → 404; часы/вкладки/баннер присутствуют. Повторная приёмка глазами
пользователя — после деплоя на mako-play.

## Этап 7.6 — жизненный цикл контента, удаление проектов, прокси для Telegram (2026-06-11) ✅

Запрос пользователя после самостоятельного деплоя: старый контент v1 невидим и неудаляем;
проекты нельзя удалить; Telegram недоступен с сервера в РФ — нужны прокси с управлением и фейловером.

- [x] `[S]` Каскадное удаление проекта: POST /projects/{slug}/delete удаляет проект + ВСЕ связанные
  данные (FK CASCADE) + файлы контента; 409-запрет отменён; подтверждение в UI
- [x] `[S]` Видимость контента: «Вне плана» показывает ВСЕ осиротевшие content (включая rejected)
  и контент архивных проектов (даже с plan_item); русские статусы; кнопка «Удалить всё (N)»;
  `delete_content_files` вынесена в services/cleanup.py; тест «ничего не теряется» (test_visibility.py)
- [x] `[S]` Прокси-подсистема: таблица proxies (url шифруется Fernet), services/proxies.py
  (parse_proxy_input понимает формат `curl --proxy "..."` и голые URL; request_via_proxy —
  автофейловер по пулу: сетевые ошибки → fail_count+1 → следующий прокси → прямой fallback;
  4xx/5xx цели ≠ ошибка прокси), страница /proxies (добавить/вкл/выкл/проверить/удалить,
  пароль маскируется), telegram.py + getMe-проверка переведены на request_via_proxy
- [x] `[оркестратор]` Приёмка прода zavod.mak-o.ru по HTTP: профиль ИИ в тему ✅, цикл до стратегии ✅,
  каскад смоук 200 ✅; найден блокер ENABLE_SCHEDULER=0 (мозг/фабрика/публикация стоят)
- [x] `[оркестратор]` Сабагентам в .claude/agents добавлен `effort: medium` (раньше думали на максимуме)

**Приёмка:** pytest 486/486 ✅; смоук 7 страниц → 200, прокси добавляется из curl-формата,
пароль не светится в HTML. На проде — после деплоя + ENABLE_SCHEDULER=1.

## Этап 7.12 — медиа-цепочка без платных звеньев + VK user token ✅ (2026-06-11)

Реализован и задеплоен (подтверждено: прод работает без ошибок). Решения — в Журнале (2026-06-11 (6)).
Состав по факту кода: цепочка `fetch_image` = Pexels → Pixabay → Openverse → Wikimedia; Pollinations
выпилен; `httpx[socks]` + свой SOCKS5; прокси с 402 авто-помечается сбойным; VK-видео через
опциональный `user_token`.

## Этап 8 — «Маркетинг — всему голова»: агентный мозг, качество и безопасность контента, UX (2026-06-11)

Источник: `logs.md` (приёмка пользователя после стабилизации прода). Сквозной принцип этапа:
**маркетинг пронизывает всё** — каждый пункт плана и каждая единица контента несут конкретную
маркетинговую цель, а не «контент ради контента». Порядок реализации = порядок приоритетов: 8.1 → 8.9.

### Правило работы (пункт 1 logs.md) — ✅ внесено в CLAUDE.md
- [x] Оркестратор сам ПРОЕКТИРУЕТ реализацию (файлы, сигнатуры, алгоритмы, схемы, edge-cases, тесты) и передаёт сабагенту готовое ТЗ «тупо написать код» — сабагент ничего не придумывает.

### 8.1 (P0) Быстрые UX-фиксы — мелкие, но бесящие ✅ (2026-06-11, 646/646)
- [x] `[оркестратор]` Шапка «проект не выбран» при выбранном проекте: корень — Jinja2-скоупинг (`{% set %}` внутри `{% for %}` не виден снаружи цикла) → `namespace()` в base.html.
- [x] `[оркестратор]` /queue: локальные `.dot-*` переопределяли контракт (planned #22c55e и published #10b981 — оба зелёные) → выровнены с контрактом: planned синий #38bdf8, published зелёный #22c55e, generating жёлтый; generated оставлен фиолетовым (отличим от published).
- [x] `[S]` /plan: POST /plan/clear/{slug}/{all|unpublished|unapproved} + меню «🧹 Очистка…» (details) с тремя кнопками и hx-confirm-описаниями; каскад через `delete_content_cascade`; «неопубликованное» = NOT EXISTS schedule published/manual_done; хелпер `_render_board_with_flash` (унифицирован флеш refresh/clear). tests/test_plan_clear.py (10).
- [x] `[S]` Стратегия: POST /projects/{slug}/strategy/reset — каскад контента plan_items → DELETE plan_items → DELETE strategies → stage='draft'; кнопка «🗑 Полный сброс стратегии» с confirm; empty-state страницы стратегии. TestStrategyReset (3).

### 8.2 (P0) Агентный маркетинговый мозг — ядро этапа
Жалобы: стратегия и план «примитивное говно», LLM игнорирует прямые указания («сделай 7 постов в неделю» → делает 3), везде продажи, «тупой чат с ИИ» вместо агента, контекст засирается и через пару дней не вывозит.
Контракт (детали проектирует оркестратор перед волной):
- **Количество — это код, не LLM.** Стратегия фиксирует `content_mix` (платформа × тип × штук/день или /неделю, по фазам). Планнер генерит РОВНО столько слотов, сколько в mix, — LLM наполняет только темы/брифы под уже созданные слоты. Указание пользователя «по N штук» физически не может быть проигнорировано.
- **Стратегия по фазам:** периоды (недели/месяцы) с целью каждой фазы, распределением контента по маркетинговым задачам (привлечение новой аудитории / удержание интереса / вывод на продажу — доля продающего контента мала и растёт постепенно), агрессивностью подачи. Пример сценария пользователя «быстро привлечь аудиторию» — из logs.md п.7.
- **Директивы пользователя — закон.** user_comment разбирается в структурированные директивы (хранятся при стратегии); агент-проверяющий сверяет результат с каждой директивой, не сошлось → переделка (ограниченное число итераций), в UI видно что учтено.
- **Агентный режим, не чат:** свой лёгкий agent loop поверх LLM-Router (наш стек, никаких LangChain/CrewAI — 4GB RAM): оркестратор-агент (декомпозиция, проверка результата, общение с пользователем) + исполнители (стратегия, план, ревизия). Контекст НЕ растёт: каждый шаг получает компактный собранный контекст (профиль, фаза, метрики-сводка, директивы, последние learnings), а не историю чата.
- **AGENTS.md завода:** `app/prompts/agents/AGENTS.md` — базовые правила мозга (русский рынок, законы, тон, запреты, принципы маркетинга) + per-project дополнение (редактируемое в UI, хранится в projects.settings). Подгружаются в каждый агентный шаг.
- [x] `[оркестратор]` Спроектировано (2026-06-11) — контракт ниже.
- [x] `[O+S]` Реализация по волнам A→E (см. контракт). **ЭТАП 8.2 ЗАВЕРШЁН (2026-06-13), pytest 687/687 ✅.**

#### Контракт 8.2 (зафиксирован оркестратором — менять только через PLAN)

**1. Схема стратегии v2 (JSON в strategies.strategy):**
```json
{
  "summary": "...", "positioning": "...",
  "activated_on": "YYYY-MM-DD",          // якорь фаз; ставится кодом при первой активации, НАСЛЕДУЕТСЯ при revise (кампания продолжается)
  "phases": [
    {"n": 1, "weeks": 2, "name": "Запуск: набор аудитории",
     "objective": "быстро набрать первичную аудиторию",
     "goal_share": {"attract": 70, "retain": 25, "sell": 5},   // % контента по маркетинговым целям
     "mix": {"telegram": {"post": 7, "video": 3}, "vk": {...}}, // штук В НЕДЕЛЮ per платформа/тип — ПОЛНЫЙ mix фазы
     "notes": "без продаж, агрессивная подача"}
  ],
  "platforms": {"telegram": {"goals": "...", "rubrics": [{"name": "...", "goal": "attract", "description": "..."}],
                 "best_times": ["09:00","18:00"], "kpi": "..."}}
}
```
Время после последней фазы → последняя фаза действует бессрочно (steady state). Легаси-стратегии
(старый плоский `content_mix` в platforms, rubrics-строки) трактуются кодом как одна бессрочная фаза
с goal_share {attract 40, retain 50, sell 10} — слой совместимости в slots.py, БД не мигрируем.

**2. Количество — код, не LLM: `app/pipeline/slots.py` (новый).**
`build_slots(project_id, platform, date_from, date_to) -> list[dict]` — детерминированная генерация слотов:
- неделя k = [activated_on+7k; +7k+6]; фаза недели — по сумме weeks фаз от якоря;
- для каждого типа с per_week=n в mix фазы: дни недели = floor(i*7/n), i=0..n-1 (равномерный разброс; n>7 → несколько в день);
- время — ротация best_times; цель слота — взвешенный round-robin по goal_share (метод наибольших остатков на неделю);
- слот = {date, time_slot, content_type, goal}; дедуп против существующих plan_items (любой статус кроме rejected) по (platform, content_type, date, time_slot);
- только даты в [date_from, date_to].
Планнер v2: brain как раньше зовёт по 1 платформе за тик, внутри — ПОНЕДЕЛЬНЫЙ цикл: слоты недели (≤~25)
→ один LLM-вызов purpose='plan' (v2): на вход слоты [{slot_id, date, weekday, content_type, goal}] + рубрики
(с фильтром по goal) + директивы + used_topics + правила; на выход {"items":[{"slot_id":N,"title","brief"}]};
код джойнит по slot_id; недостающие slot_id → 1 retry списком; всё ещё нет → вставка плейсхолдера
title='(тема не сгенерирована — нажмите 🔄)' brief=NULL. ИТОГ: число пунктов плана ФИЗИЧЕСКИ равно mix.

**3. БД (идемпотентные миграции):** `plan_items` + колонка `goal TEXT` (NULL ок);
новая таблица `directives(id, project_id→projects CASCADE, scope CHECK('strategy','plan','content'),
text NOT NULL, parsed JSON, status CHECK('active','done','dismissed') DEFAULT 'active',
created_at DEFAULT (datetime('now','localtime')))`.

**4. Директивы пользователя — закон.** user_comment при revise → purpose='directives_parse'
(LLM разбивает на атомарные директивы; количественные парсятся в parsed
{platform?, content_type?, per_week: N} — их применяет КОД как override поверх mix фазы в slots.py).
Активные директивы включаются компактным блоком в КАЖДЫЙ промпт мозга (стратегия/план/контент).
Проверяющий шаг: после генерации стратегии purpose='strategy_check' (LLM-судья: стратегия vs директивы
vs базовые правила → {"ok": bool, "violations": ["..."]}); не ок → регенерация с перечнем нарушений,
максимум 2 итерации; после — стратегия принимается, violations пишутся в strategy.check_warnings
(видны в UI жёлтым). Это и есть «оркестратор проверяет исполнителей».

**5. Правила мозга (AGENTS.md):** `app/prompts/agents/AGENTS.md` — базовые правила
(рынок РФ и закон, дозирование продаж, принципы контент-маркетинга, тон, запрет воды);
`app/prompts/agents/forbidden_ru.md` — запретные темы (общий с 8.3-цензором);
per-project дополнение — projects.settings['agent_rules'] (текст, UI позже).
Хелпер `app/pipeline/prompts.py::load_rules(project_settings=None) -> str` — конкатенация; подставляется
плейсхолдером <<RULES>> в strategy/plan/item-промпты.

**6. Контекст не растёт (агентный режим вместо чата):** каждый шаг мозга получает СВЕЖИЙ компактный
контекст: профиль + текущая фаза + сводка метрик (агрегаты, не сырьё) + активные директивы + ≤10 learnings
+ ≤30 последних тем. Никакой истории чата нигде не копится; «память» = таблицы directives/learnings/metrics.
Тяжёлых агент-фреймворков НЕ брать (4GB RAM, бюджет) — agent loop = детерминированный код + LLM-судья.

**Волны 8.2** (после каждой — полный pytest + контрольный прогон оркестратора):
- [x] **A `[S]`** Фундамент: миграция БД (plan_items.goal — миграция 6, directives — миграция 7 + SCHEMA), AGENTS.md + forbidden_ru.md + load_rules() (кеш базы, project-часть на лету), FAKE-заглушки 'directives_parse'/'strategy_check', tests/test_brain_foundation.py (13). **✅ 656/656 (2026-06-11)**
- [x] **B `[O]`** Стратегия v2: strategy.txt/strategy_revise.txt v2 + directives_parse.txt + strategy_check.txt; build_strategy: директивы из user_comment → таблица directives, RULES/DIRECTIVES в промптах, _validate_phases (нормализация goal_share, синтез фазы при отсутствии), наследование activated_on, материализация компат-content_mix из фазы 1, strategy_check-цикл (≤2 регенерации → check_warnings); _FAKE_STRATEGY(+REVISE) v2; UI: таблица фаз, блок директив, warnings; TestStrategyV2 (8). Известный долг: ручная правка рубрик в строковом режиме (объекты теряют goal — доделать в D). **✅ 663/663 (2026-06-12, контрольный прогон оркестратора)**
- [x] **C `[O]`** Слоты и планнер v2: app/pipeline/slots.py (build_slots: фазы по неделям от якоря activated_on→created_at→today, mix→дни floor(i*7/n), ротация best_times + развод коллизий +30мин, директивы-override per_week, goal по goal_share наибольшими остатками, легаси-слой без phases, дедуп против существующих items); planner.py: _generate_plan_impl — понедельные группы слотов → chat('plan') на группу → джойн по slot_id → retry недостающих → плейсхолдер «(тема не сгенерирована — нажмите 🔄)»; date/time/type/goal — ИЗ СЛОТА, не из LLM; plan.txt v2 (RULES/DIRECTIVES/RUBRICS/SLOTS); _FAKE_PLAN 60 слотов; test_slots.py (10). Циклический импорт planner↔slots решён ленивым импортом. **✅ 673/673 + независимый смоук оркестратора: mix 7/нед × 2 нед → ровно 14 пунктов, 7/нед на 7 разных днях, goal у всех (2026-06-12)**
- [x] **D `[S]`** UI директив: блок «Директивы» на странице стратегии (список + кнопки «✓ Выполнена»/«✕ Отклонить» → POST /strategy/directives/{id}/{done|dismissed}, 303; чужая/несуществующая → 404, неизвестный action → 422), check_warnings жёлтым (уже было в B), форма revise поясняет «комментарий станет директивами», парсинг рубрик с целью «Название | goal | описание» в strategy_edit (закрыт долг волны B — объекты-рубрики больше не теряют goal; невалидный goal → retain; строка без «\|» остаётся легаси-строкой), goal-бейдж в модалке плана (attract/retain/sell/brand → рус. подпись). +10 тестов (TestStrategyDirectives 6, TestStrategyEditRubricsGoal 2, модалка goal 2). **✅ 683/683 (2026-06-13)**
- [x] **E `[S]`** E2e (tests/test_e2e_directives.py, 4 теста): директива per_week=7 перебивает mix фазы 3→7 (generate_plan → ровно 7 telegram/post за неделю, 7 разных дней, goal у всех) — жалоба «говорю 7, делает 3» закрыта ЧЕРЕЗ директиву пользователя; build_strategy(revise) создаёт фазовую v2 + активную директиву с per_week; легаси-стратегия (плоский content_mix, строковые рубрики) генерит план без падений (content_mix post=4 → 4 пункта). DEPLOY.md дополнен миграциями 6 (plan_items.goal) и 7 (directives) + про слой совместимости легаси. **✅ 687/687 (2026-06-13)**

### 8.3 (P0) Безопасность контента — закон РФ и 18+ ✅ (2026-06-13, pytest 697/697)
Инциденты: «кастрация» в тексте, мужская пара как романтическая сцена в видео. Риск — статья, для РФ-рынка недопустимо.
- [x] `[S]` Слой «цензор»: УРОВЕНЬ 1 (превентивный) — секция `## Правила и запреты / <<RULES>>` (load_rules = AGENTS.md + forbidden_ru.md) добавлена в item_post.txt, item_story.txt, video_script.txt, RULES подставляется из from_plan/video_script. УРОВЕНЬ 2 (реактивный) — `app/pipeline/censor.py::check_content(text, context) -> (ok, reason)` через purpose='censor' (промпт censor.txt + forbidden_ru.md), `CensorError`; пост-проверка с регенерацией ≤2 → потом plan_items.status='error' («Цензор отклонил: …»). FAKE_CENSOR по умолчанию пропускает. **Дизайн-решение: fail-open при ТЕХНИЧЕСКОМ сбое цензора** (LLMError/мусор → пропуск + warning; блок только при явном verdict='block') — основная защита уровень 1, цензор не валит конвейер из-за своих сбоев.
- [x] `[S]` Цензор применяется к: текстам постов (_generate_text), историям (overlay+caption+keywords, _generate_story), видео-сценариям (текст сцен + keywords ДО поиска футажей, generate_video_script). tests/test_censor.py (10). **697/697 ✅**

### 8.4 (P1) Качество контента под маркетинг ✅ (2026-06-13, pytest 702/702)
- [x] `[S]` У каждого plan_item — маркетинговая цель (`goal: attract|retain|sell|brand`, заполняется планнером из goal_share фазы — волна 8.2C). Новый `app/pipeline/goals.py` (GOAL_LABELS + goal_label/goal_guidance — единый источник инструкций по целям). Промпты item_post/item_story/video_script получают `<<ITEM_GOAL_LABEL>>` + `<<GOAL_GUIDANCE>>` (from_plan берёт `item["goal"]`, video — через новый параметр `generate_video_script(..., goal=None)`). attract/retain/brand явно НЕ продают; sell — единственный с прямым оффером.
- [x] `[S]` Ревизия промптов генерации: секция «Маркетинговая цель этой публикации» + пункт в Требованиях «продающий оффер ТОЛЬКО при цели=продажа». tests/test_goals_marketing.py (5). **702/702 ✅**

### 8.5 (P1) Сторис 2.0 — осмысленные, как у людей
Сейчас: отдельно фото + отдельно текст. Надо: изображение/видео С НАЛОЖЕННЫМ текстом (и смайлики), осмысленные форматы.
- [x] `[оркестратор]` Спроектировано (2026-06-13) — контракт ниже. **Рендер через ASS/libass (НЕ drawtext) — подтверждён экспериментом: кириллица + плашка (BorderStyle=3) + переносы (\N) + позиция (\an) на фоне 1080×1920 рендерятся одним кадром JPG, читается отлично.** drawtext отвергнут (мучается с кириллицей/путями/многострочностью на Windows; libass уже отлажен в render.py). Эмодзи в НАЛОЖЕНИИ не рендерим (libass без emoji-шрифта даёт тофу, на Linux-сервере seguiemj нет) — `_strip_emoji` вырезает их из текста слайда; в caption поста эмодзи остаются.
- [x] `[S]` **ВОЛНА A (ядро) ✅ (2026-06-13, 711/711):** config STORY_FONT/STORY_FONT_SIZE; story_render.py (render_story_slides через ASS+ffmpeg, _strip_emoji, фон fetch_image→fallback lavfi); item_story v2-схема (slides/story_type/position) + промпт; _parse_item_story v2+легаси, _normalize_story_slides, _generate_story переписан (цензор-петля и GOAL/RULES сохранены, blob покрывает caption+слайды); test_story_render.py (9). **Контрольный смоук оркестратора реальным ffmpeg: 2 слайда 1080×1920 с кириллицей, плашкой, переносом \N, позицией bottom — отрендерены и проверены глазами ✅.**
- [x] `[S]` **ВОЛНА B (публикация+UI) ✅ (2026-06-13, 718/718):** роут раздачи слайдов GET /files/stories/{content_id}/{idx}.jpg (по соглашению media/{id}/slide_N.jpg), serve_story_image → slide_1 с легаси-фоллбэком story.jpg; _get_manual_pending отдаёт slide_indexes + copy_text=caption для story; ручная очередь (queue/index.html) и модалка (queue/_modal.html) показывают ВСЕ слайды пронумерованно со скачиванием (легаси-фоллбэк на одну картинку); test_story_publish.py (7). delete_content_files уже rmtree папки media/{id} — мусор слайдов/bg/ass чистится. VK stories авто — НЕ делали (опционально, story всегда manual). **ЭТАП 8.5 ЗАВЕРШЁН (A+B).**

#### Контракт 8.5 (зафиксирован оркестратором — менять только через PLAN)

**1. Схема item_story v2 (JSON, purpose='item_story'):**
```json
{
  "story_type": "card" | "carousel",
  "slides": [
    {"text": "Короткий текст слайда (1–7 слов, допустим \\n для строк)",
     "image_keywords": ["english", "keyword"], "position": "center"|"top"|"bottom"}
  ],
  "caption": "Подпись поста (эмодзи допустимы — идут в текст поста, не в наложение)",
  "features": {"hook_type","topic","length","format":"story","ab_variant"}
}
```
card → ровно 1 слайд; carousel → 2–5 слайдов. goal=sell → допустим финальный слайд-CTA (решает LLM по <<GOAL_GUIDANCE>>). **Слой совместимости:** если LLM вернул старый формат (нет `slides`, но есть `overlay_text`/`image_prompt`) — код синтезирует 1 слайд {text: overlay_text, image_keywords, position:"center"}.

**2. Рендер `app/pipeline/story_render.py` (новый):**
- `render_story_slides(slides, out_dir, *, font, font_size) -> list[Path]` — на каждый слайд: `fetch_image(slide["image_keywords"], bg)` (фон через assets.fetch_image, FAKE_ASSETS сам обрабатывается) → ASS (1 Dialogue, стиль: Fontname=font, Fontsize, Bold, PrimaryColour белый, BorderStyle=3 + BackColour=&H96000000 плашка, Alignment по position: top=8/center=5/bottom=2, MarginV) → ffmpeg `-loop 1 -i bg -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles='<esc>'" -frames:v 1 slide_N.jpg`. Возврат — пути по порядку.
- `_strip_emoji(text)` — regex по Unicode-диапазонам эмодзи, применяется к тексту ДО ASS.
- Переиспользовать `render.escape_subtitles_path`, `_run_ffmpeg`-паттерн, `settings.FFMPEG_BIN`. В тестах мокать `_run_ffmpeg` + `fetch_image`.

**3. config:** `STORY_FONT` (str, default "Arial"; сервер — DejaVu Sans/Liberation Sans Bold), `STORY_FONT_SIZE` (int, default 96).

**4. from_plan._generate_story:** парсинг v2 (slides) с фоллбэком v1 → `_strip_emoji` по text слайдов → `render_story_slides` → `files.slides=[пути]`, `files.image_path`=первый слайд (превью/совместимость). Цензор-петля (8.3) остаётся: blob = caption + тексты слайдов + keywords. caption в texts[platform].

**5. Тесты `tests/test_story_render.py`:** _strip_emoji; ASS-стиль по position (alignment); render_story_slides зовёт ffmpeg N раз (мок) → N путей; from_plan v2 (slides → files.slides) и v1-фоллбэк (overlay_text → 1 слайд).

### 8.6 (P1) Субтитры и озвучка — синхрон и пунктуация
- [x] `[O]` **ВОЛНА 1 ВЫПОЛНЕНА (2026-06-13, 722/722).** Субтитры: пунктуация восстановлена (`tts._reattach_punctuation` — переналожка оригинальных whitespace-токенов на тайминги edge-tts при совпадении числа); накопительное появление «по мере речи» — `build_ass` ПЕРЕПИСАН на TikTok-стиль: по одному Dialogue на слово, событие k показывает слова [0..k], текущее — жёлтым inline-тегом `{\1c&H..&}`, прошлые — белым (karaoke `{\k}` убран — он не даёт трёх состояний невидимо→жёлтое→белое); рассинхрон устранён — `render._concat_voice` переписан с concat на `adelay+amix(normalize=0)` (каждая сцена якорится в свой точный глобальный offset → нет накопления mp3 priming-тишины); ручка `SUBTITLE_OFFSET_SEC` (config float, default 0.0) для подстройки систематического лида при приёмке. Реальные ffmpeg-тесты рендера зелёные (libass принял новый ASS, adelay+amix работает).
- [x] `[S]` **ВОЛНА 2 ВЫПОЛНЕНА (2026-06-13, 730/730).** Ревизия RU-голосов: edge-tts для ru-RU даёт РОВНО ДВА нейроголоса (DmitryNeural/SvetlanaNeural); мультиязычные говорят по-русски с акцентом — НЕ добавляем; дефолт Svetlana оставлен. Добавлено управление темпом/тоном: `tts.py` rate/pitch (валидаторы `_valid_rate`/`_valid_pitch` — формат `±N%`/`±NHz`, невалидное → дефолт `+0%`/`+0Hz`) прокинуты в `Communicate`; настройки проекта `tts_rate`/`tts_pitch` (db.DEFAULT_PROJECT_SETTINGS); `produce.py` читает и пробрасывает; панель запуска (`_launch.html`) — два select «Темп речи»/«Тон голоса»; кнопка «▶ Прослушать» и роут `/tts/preview/{voice}?rate=&pitch=` учитывают темп/тон (кеш per rate/pitch). **ЭТАП 8.6 ЗАВЕРШЁН (волны 1+2).**

### 8.7 (P2) Авто-сбор метрик
- [ ] `[S]` collect_metrics: интервальный джоб раз в N часов (настройка, default 6, джиттер чтобы не попадать в блок) вместо daily 03:00; кнопка «Обновить сейчас» на /analytics (HTMX, с индикатором и rate-limit от дабл-клика).

### 8.8 (P2) Живой UI без скачков страницы
Жалоба: изменений не видно без F5; /proxies перезагружает страницу с прыжком вверх.
- [ ] `[оркестратор]` Спроектировать: SSE (`EventSource` + endpoint на FastAPI, без вебсокетов — проще и хватает) либо HTMX-поллинг фрагментов с `hx-swap="morph"` (idiomorph) — выбрать одно решение для всех вкладок.
- [ ] `[S]` Применить: /plan и /queue (статусы шахматок), /proxies (тумблеры без перезагрузки), дашборд (счётчики), бейджи навигации. Никаких прыжков скролла.

### 8.9 (P3) Трендовая музыка через платформы (research)
Хотелка: музыка не вшита в видео (права), а прикреплена средствами платформы (как при ручной публикации в IG/VK), трендовая, тихая.
- [ ] `[S]` Research честно: VK API audio закрыт для сторонних приложений с 2016; IG Graph API музыку в Reels программно не даёт; TG — нет музыки. Задокументировать что возможно; вероятный итог — подсказка «какую музыку прикрепить» в ручной очереди (тренды не достать без нарушения ToS).
- [ ] `[S]` Громкость ВСТРОЕННОЙ фоновой музыки видео — настройка в проекте (default тихая, «лёгкое звучание»), применяется в render (volume перед ducking).

**Приёмка этапа 8:** стратегия с фазами и обоснованным mix, «сделай 7 постов в неделю» даёт ровно 7, директивы видимо учтены; цензор режет 18+/закон РФ; сторис — слайды с наложенным текстом; субтитры пословно с пунктуацией; UI: селектор проекта корректен, цвета различимы, кнопки очистки работают, страницы не прыгают; метрики обновляются сами и по кнопке. Полный pytest зелёный.

## Справочник бесплатного стека

| Задача | Решение |
|---|---|
| LLM | LLM-Router → DeepSeek (платный якорь) + free: Gemini API, Groq, OpenRouter, Mistral |
| Озвучка RU | edge-tts (Dmitry/Svetlana), word timings бесплатно |
| Футажи/фото | Pexels API, Pixabay API, Openverse API, Wikimedia Commons (всё бесплатно; обход IP-блоков — свой SOCKS5 на VPS) |
| Картинки AI | ~~Pollinations.ai~~ (стал платным, выпилен 2026-06-11); Gemini image gen — резерв |
| Музыка | Pixabay Music, YouTube Audio Library |
| Рендер | ffmpeg (filter graph, без moviepy) |
| Сервисы | SQLite, APScheduler, FastAPI+Jinja2+HTMX, Chart.js |

## Риски

- YouTube API без audit → видео private (полуручной режим до одобрения заявки)
- 30GB SSD → обязательная ротация медиа
- Free-тиры меняются → пул аккаунтов и фоллбэки в роутере
- IG-автоматизация мимо Graph API = бан → только ручная очередь
- Ключи друзей → шифрование в SQLite (Fernet), кабинет за паролем, HTTPS

## Журнал решений

- 2026-06-11 (6): **Этап 7.12 — медиа-цепочка без платных звеньев (решения пользователя):** (а) Pollinations выпилен полностью — 402 даже с токеном, бесплатного тира нет; (б) Pexels/Pixabay остаются (бесплатны, блок по IP обходим прокси); (в) пользователь поднимает свой SOCKS5 на VPS в Нидерландах (dante) → завод получил поддержку socks5 (`httpx[socks]`); (г) новые бесплатные источники без ключей: Openverse API и Wikimedia Commons API — цепочка `fetch_image` = Pexels → Pixabay → Openverse → Wikimedia; (д) локальный ffmpeg-фоллбэк «карточка с градиентом» — ОТКЛОНЁН пользователем; (е) прокси, ответивший 402, считается исчерпанным (fail_count+1, дальше по пулу) — 402 от прокси-провайдера ≠ 402 целевого сайта.
- 2026-06-11 (6): **VK-видео — через пользовательский токен:** `video.save` недоступен групповым токенам (ошибка 27, ограничение VK API). В credentials площадки VK добавлен опциональный `user_token` (админ сообщества, права video+wall+offline, получение через vkhost/Kate Mobile); видео грузится им, посты/фото — прежним групповым. Без user_token видео падает с понятной подсказкой.
- 2026-06-11 (5): **Картинки — стоковые фото вместо ИИ-генерации**: Pollinations закрыл анонимный доступ (402 со всех IP) — на нулевом бюджете картинки берём из Pexels/Pixabay Photo API (бесплатные ключи, лицензия позволяет): цепочка `fetch_image` = Pollinations (только с токеном) → Pexels → Pixabay. LLM отдаёт `image_keywords` (2–4 англ. слова) для стокового поиска.
- 2026-06-11 (5): **Озвучка и субтитры настраиваются per-project** (панель запуска): голос по умолчанию Светлана (женский, ru), варианты Дмитрий/автовыбор ИИ; субтитры по умолчанию белый шрифт + чёрное обрамление (толщина 5) + жёлтая karaoke-подсветка, всё в project.settings. UI: живой CSS-предпросмотр субтитров + «▶ Прослушать» голос (GET /tts/preview/{voice}, кеш mp3).
- 2026-06-11 (4): **Все скачивания ассетов — с прокси-фейловером** (Pexels CDN 403, Pixabay reset, Pollinations 402 с РФ-IP): `assets.py::_http_get/_download_stream` после провала прямого запроса перебирают активные http-прокси пула. Pollinations: при 402 ретрай без `nologo=true` + опциональный `POLLINATIONS_TOKEN` в .env.
- 2026-06-11 (4): **Удаление пунктов плана — каскадное для proposed/rejected/error** (отменяет «error неприкосновенны» от (3)): кнопка «Удалить» в модалке /plan видна для этих статусов даже с контентом — schedule/content/файлы удаляются через `cleanup.delete_content_cascade`. generating/generated — по-прежнему 422 («управляйте в Публикации»).
- 2026-06-11 (4): **refresh_plan чистит всё неодобренное**: удаляет proposed + rejected + error (с каскадом контента) в окне горизонта; остаются только approved/generating/generated — по требованию пользователя «после обновления остаётся только одобренное».
- 2026-06-11 (3): **Разметка публикаций — конвертация на этапе публикации, не доверие промпту**: паблишеры конвертируют через `services/textfmt.py` — TG в HTML-теги (parse_mode=HTML), VK/IG/Дзен в чистый текст; промпт дополнительно запрещает markdown (двойная защита).
- 2026-06-11 (3): **Каждый текстовый пост — с картинкой** («посты без изображений пролистывают»): LLM отдаёт `image_prompt` в тему поста → Pollinations → `media/{id}/post.jpg`; провал картинки НЕ валит пост. TG: фото+caption (>1024 — фото отдельно от текста), VK: saveWallPhoto-attachment.
- 2026-06-11 (3): **«Текущий проект» — cookie `current_project`**: выбирается открытием проекта, переключатель в шапке; /plan и /queue показывают только его (без выбора — всё). Вкладка «Проекты» → «Проект», ведёт сразу в выбранный.
- 2026-06-11 (3): **Перегенерация контент-плана трогает только proposed**: refresh_plan (весь/платформа/тип) пересоздаёт только не одобренные пункты в окне горизонта; approved/generating/generated/error неприкосновенны.
- 2026-06-11 (3): **edge-tts: апгрейд 7.2.8 + прокси-фейловер** вместо смены TTS-провайдера; `boundary="WordBoundary"` обязателен для karaoke-субтитров.
- 2026-06-11 (2): **Удаление проекта — каскадное** (решение пользователя, отменяет запрет от 2026-06-10): DELETE проекта удаляет весь его контент, планы, стратегии, расписание и файлы. FK в SQLite уже каскадные, файлы чистятся отдельно после коммита.
- 2026-06-11 (2): **Прокси для Telegram** — пул HTTP-прокси в БД (Fernet), управление на /proxies, автофейловер в request_via_proxy (services/proxies.py); все вызовы api.telegram.org (паблишер + getMe-проверка) только через него. socks5 парсится и хранится, но требует httpx[socks] — пока не ставим.
- 2026-06-11: **Старый флоу (идеи → ревью → календарь → ручная очередь) удалён из веба окончательно** — весь рабочий цикл идёт через две вкладки «Контент-план» и «Публикация»; ручная публикация — секция внутри «Публикации». Pipeline-модули старого флоу оставлены (используются фабрикой внутренне). Старые e2e-сценарии A–D удалены, новый флоу покрывает сценарий E.
- 2026-06-11: **Единая временная база платформы — локальное время сервера.** planned_at хранит локальное намерение из плана, process_due сравнивает с datetime.now(), часы в шапке показывают то же. SQLite datetime('now') (UTC) для сравнений с planned_at запрещён.

- 2026-06-10: Браузер-автоматизация отложена (этап 6, только Gemini, только по желанию). Основа — легальные free API.
- 2026-06-10: LLM-Router — отдельный независимый проект со своим кабинетом. LiteLLM отвергнут (Postgres, тяжёлый, нет нужного UX с приоритетом бесплатных).
- 2026-06-10: Рендер — чистый ffmpeg без moviepy (экономия RAM на 4GB сервере).
- 2026-06-10: Субтитры — из WordBoundary edge-tts, Whisper не нужен (текст известен).
- 2026-06-10: **Проекты — динамические, в БД с полным CRUD через дашборд** (решение пользователя: «контент-завод как отдел продвижения под каждый проект, добавлять/удалять проекты сам»). projects.yaml отменён. Per-project настройки площадок и креденшелы — в таблице project_platforms, токены шифруются Fernet. Удаление проекта запрещено при наличии контента (только архив).
- 2026-06-10: E2E-проверки этапа 2 — без реальных токенов соцсетей: FAKE_LLM=1 (детерминированные заглушки в llm.py) + dry-run-режим паблишеров. Реальные токены — после полной реализации.
- 2026-06-10 (волна 5): TTS — per-сцена mp3 (проще нарезка футажей по фразам), тайминги слов приводятся к глобальной шкале ролика; duration сцены — из ffprobe. Keywords сцен для поиска футажей LLM выдаёт СТРОГО на английском. Картинки — Pollinations.ai без ключа (Gemini image gen через роутер отложен). Рендер — 5 раздельных проходов ffmpeg вместо одного filter_complex (бюджет 4GB RAM). Статусы видео: text_review (текст сценария) → production (рендер, ≤3 попыток, ошибки в files.produce_error) → review (готовый ролик) → approved + планирование. Пути в content.files — абсолютные.
- 2026-06-10 (прод): systemd-юнит завода — БЕЗ `EnvironmentFile=` (.env читает само приложение через python-dotenv): systemd не удаляет inline-комментарии и испорченные значения побеждают dotenv → был 404 от роутера на проде. В config.py добавлен харденинг (URL/ключи/bool/int — первый токен значения). DEPLOY.md обоих проектов переведены на конкретные значения сервера (mako-play, /srv/KontentZavod, /srv/LLMRouter, router.mak-o.ru) — сессионные переменные отменены.
- 2026-06-10 (волна 7): TG-метрики через Telethon отложены до реальных ключей (новая зависимость + api_id/api_hash) — TG вводится вручную в /analytics. A/B — отдельной кнопкой и отдельным промптом script_ab.txt (generate_script не тронут): LLM сразу отдаёт оба полных варианта текстов, различающихся только хуком, — без сборки строк в коде. Analyzer держит ≤10 активных инсайтов на проект (деактивация по weight) и не может трогать reject-learnings. UNIQUE(schedule_id,date) на metrics → сбор и ручной ввод идемпотентны (INSERT OR REPLACE).
- 2026-06-10 (волна 6): VK-клипы (`shortVideo.create`) недоступны обычным токенам → видео публикуется в стену через `video.save` + `wallpost=1`. YouTube — без google-api-python-client (тяжёлый): чистый httpx (oauth refresh → resumable init → PUT). Дневная квота YouTube — собственный счётчик published-записей за сегодня + новое исключение `PublishDeferred(retry_at)`: process_due переносит planned_at без увеличения attempts (квота ≠ ошибка).
- 2026-06-10: Экономия токенов — схема «оркестратор + сабагенты» (решение пользователя): главная сессия только оркестрирует, работу выполняют сабагенты coder-simple (haiku) / coder (sonnet) / architect (opus). От переключения модели главной сессии через `"model"` в settings.json отказались. Stop-хук следит за свежестью STATE.md.
