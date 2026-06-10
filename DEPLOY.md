# Деплой КонтентЗавода на VPS

Копипаст-инструкция. Все значения уже подставлены под боевой сервер — команды
готовы к выполнению без дополнительных правок.

> **Сервер:** `ssh root@mako-play`
> **Предварительное условие:** LLM-Router уже задеплоен и работает
> (`systemctl is-active llmrouter` → active). Если нет — сначала выполни `LLMRouter/DEPLOY.md`.

---

## 1. Первичный деплой

### 1.1 Проверка Python, ffmpeg и создание системного пользователя

```bash
python3 --version          # нужен 3.10+; если ниже — см. примечание
id kontentzavod 2>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin kontentzavod
```

> Если Python < 3.10: `apt update && apt install -y python3.10 python3.10-venv python3.10-distutils`

> **ffmpeg обязателен** — видеоконвейер работает на проде:
> ```bash
> apt install -y ffmpeg
> ffmpeg -version   # проверка
> ```

> **Пользователь:** используем отдельного пользователя `kontentzavod` — независимо от `llmrouter`.
> Это изолирует права доступа к файлам данных каждого сервиса.

### 1.2 Создание директории и папок данных

```bash
mkdir -p /srv/KontentZavod/data/outbox
chown -R kontentzavod:kontentzavod /srv/KontentZavod
```

### 1.3 Заливка файлов

**Вариант A — rsync с Windows-машины** (выполняется локально в PowerShell):

```powershell
rsync -avz --exclude ".venv" --exclude "data" --exclude ".env" `
  "A:/DevAI/Projects/KontentZavod/" `
  root@mako-play:/srv/KontentZavod/
```

После rsync — обязательно восстановить владельца (systemd запускает сервис от `kontentzavod`, root-файлы он не прочитает):

```bash
chown -R kontentzavod:kontentzavod /srv/KontentZavod
```

> Для scp (без исключений — медленнее):
> ```powershell
> scp -r "A:\DevAI\Projects\KontentZavod\app" root@mako-play:/srv/KontentZavod/
> scp "A:\DevAI\Projects\KontentZavod\requirements.txt" root@mako-play:/srv/KontentZavod/
> scp "A:\DevAI\Projects\KontentZavod\.env.example" root@mako-play:/srv/KontentZavod/
> ```

**Вариант B — git clone** (когда репозиторий появится):

```bash
sudo -u kontentzavod git clone https://github.com/ТВОЙ_REPO/kontentzavod.git /srv/KontentZavod
```

### 1.4 Создание venv и установка зависимостей

```bash
sudo -u kontentzavod python3 -m venv /srv/KontentZavod/.venv
sudo -u kontentzavod /srv/KontentZavod/.venv/bin/pip install --upgrade pip
sudo -u kontentzavod /srv/KontentZavod/.venv/bin/pip install -r /srv/KontentZavod/requirements.txt
```

### 1.5 Создание .env на сервере

> **Правило комментариев в .env** — соблюдай ВСЕГДА:
> ```
> # ПРАВИЛЬНО — комментарий на отдельной строке:
> # Это мой ключ
> LLM_ROUTER_BASE_URL=http://127.0.0.1:8100/v1
>
> # НЕПРАВИЛЬНО — inline-комментарий в той же строке:
> LLM_ROUTER_BASE_URL=http://127.0.0.1:8100/v1  # этот хвост уедет в приложение!
> ```
> **python-dotenv** (прямой запуск) комментарии отсекает корректно. Но при
> ручном `nano /srv/KontentZavod/.env` легко случайно добавить inline-комментарий —
> а это ломает URL/ключи (см. типовую проблему «Роутер отвечает 404» в разделе 6).
> Правило: **комментарии ТОЛЬКО на отдельных строках**, без исключений.

```bash
tee /srv/KontentZavod/.env > /dev/null << 'EOF'
# Путь к SQLite-базе данных
DB_PATH=data/kontentzavod.db

# Директория данных (outbox, видео, кэш)
DATA_DIR=data

# LLM-Router: оба варианта рабочие.
# Рекомендуемый (роутер на том же сервере, без лишнего сетевого прогона):
LLM_ROUTER_BASE_URL=http://127.0.0.1:8100/v1
# Альтернатива (через внешний домен — тоже работает):
# LLM_ROUTER_BASE_URL=https://router.mak-o.ru/v1

# Виртуальный ключ роутера — получить в https://router.mak-o.ru/admin → Ключи → Выпустить
LLM_ROUTER_KEY=СЮДА_ВСТАВИТЬ_КЛЮЧ_ОТ_РОУТЕРА

# Fernet-ключ шифрования токенов площадок
# Сгенерировать: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY=СЮДА_ВСТАВИТЬ_НОВЫЙ_КЛЮЧ

# FAKE_LLM=0 на проде (1 только для тестов без сети)
FAKE_LLM=0

# PUBLISH_DRY_RUN=1 до ввода реальных токенов площадок через дашборд.
# Переключить на 0 ТОЛЬКО после добавления токенов TG/VK через /projects → площадки.
PUBLISH_DRY_RUN=1

# Планировщик: 1 на проде (запускает APScheduler каждую минуту)
ENABLE_SCHEDULER=1

# ── Видеоконвейер ──
# FAKE_TTS=1 → не ходить в сеть, тишина через ffmpeg + синтетические тайминги
FAKE_TTS=0
# FAKE_ASSETS=1 → не ходить в сеть, плейсхолдеры через ffmpeg lavfi
FAKE_ASSETS=0

# API-ключи медиастоков (Pexels и Pixabay — бесплатные тиры)
PEXELS_API_KEY=
PIXABAY_API_KEY=

# Сколько дней хранить финальные ролики после публикации (SSD 30GB — не жадничай)
MEDIA_RETENTION_DAYS=14

# Пути к бинарникам ffmpeg (apt install -y ffmpeg)
FFMPEG_BIN=ffmpeg
FFPROBE_BIN=ffprobe

# YouTube Data API v3: 10 000 юнитов/день, videos.insert = 1600 юнитов → ~6 загрузок
YOUTUBE_DAILY_LIMIT=6
EOF
chown kontentzavod:kontentzavod /srv/KontentZavod/.env
chmod 600 /srv/KontentZavod/.env
```

Заполни обязательные значения:

```bash
# Сгенерировать FERNET_KEY:
sudo -u kontentzavod /srv/KontentZavod/.venv/bin/python3 \
  -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Скопируй вывод и вставь в .env:
nano /srv/KontentZavod/.env
```

**Важно после первого деплоя:**

- Сгенерируй новый `FERNET_KEY` командой выше — не оставляй тестовый.
- ⚠️ **FERNET_KEY менять нельзя после добавления токенов площадок!** Если сменишь ключ,
  все сохранённые токены TG/VK/etc не расшифруются — придётся заново ввести их через
  дашборд `/projects` → секция «Площадки» для каждого проекта.
- Получи `LLM_ROUTER_KEY` в кабинете роутера (https://router.mak-o.ru/admin → Ключи → Выпустить).
- `PUBLISH_DRY_RUN=1` оставь до момента, когда введёшь реальные токены в дашборде.
  После ввода токенов переключи на `0` и перезапусти сервис.

---

## 2. systemd unit

> **Почему нет `EnvironmentFile=` в юните:**
> systemd **не удаляет inline-комментарии** при чтении EnvironmentFile.
> Строка `LLM_ROUTER_BASE_URL=http://127.0.0.1:8100/v1 # пояснение` уедет в приложение
> ЦЕЛИКОМ с хвостом → openai SDK строит мусорный URL → роутер отвечает 404 на все запросы.
> Приложение само читает `.env` через **python-dotenv** (комментарии режет корректно),
> поэтому `EnvironmentFile=` в юните не нужен.

```bash
tee /etc/systemd/system/kontentzavod.service > /dev/null << 'EOF'
[Unit]
Description=КонтентЗавод — генерация и публикация контента
After=network.target

[Service]
Type=simple
User=kontentzavod
Group=kontentzavod
WorkingDirectory=/srv/KontentZavod
ExecStart=/srv/KontentZavod/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8200 --workers 1
Restart=always
RestartSec=5

# Ограничение памяти: видеоконвейер (ffmpeg-рендер) требует до 1.5GB.
# При 4GB RAM: роутер ~512MB + завод ~1536MB + ОС ~1GB = в пределах нормы.
MemoryMax=1536M

# Безопасность
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/srv/KontentZavod/data

[Install]
WantedBy=multi-user.target
EOF
```

Активация и запуск:

```bash
systemctl daemon-reload
systemctl enable --now kontentzavod
```

Проверка:

```bash
systemctl status kontentzavod
curl -s http://127.0.0.1:8200/
# Ожидаемый ответ: HTML страница дашборда
```

---

## 3. Caddy — добавление поддомена завода

> **Не заменяй существующий Caddyfile!** Только добавь блок ниже.

### 3.1 DNS

Добавь A-запись у своего регистратора:

```
ДОМЕН_ЗАВОДА  →  IP_ТВОЕГО_VPS
```

> **ДОМЕН_ЗАВОДА** — подставь свой домен из `/etc/caddy/Caddyfile`
> (блок с `reverse_proxy 127.0.0.1:8200`). Пример: `content.mak-o.ru`.

Убедись, что запись применилась: `nslookup ДОМЕН_ЗАВОДА`

### 3.2 Добавление блока в Caddyfile

Открой существующий Caddyfile:

```bash
nano /etc/caddy/Caddyfile
```

Добавь в **конец файла**:

```caddyfile
ДОМЕН_ЗАВОДА {
    # ⚠️  ДАШБОРД БЕЗ АУТЕНТИФИКАЦИИ!
    # Встроенный логин появится в одной из следующих волн.
    # До тех пор ОБЯЗАТЕЛЬНО защити basic_auth:
    basic_auth {
        # Сгенерировать хэш пароля: caddy hash-password
        # Пример: caddy hash-password --plaintext "МойПароль123"
        # Вставь результат вместо BCRYPT_HASH ниже:
        admin BCRYPT_HASH
    }
    reverse_proxy 127.0.0.1:8200
}
```

> Замени `ДОМЕН_ЗАВОДА` на свой домен (найди в `/etc/caddy/Caddyfile` блок с `reverse_proxy 127.0.0.1:8200`).

> **Генерация bcrypt-хэша для Caddy:**
> ```bash
> caddy hash-password --plaintext "ВашПароль"
> # Скопируй вывод (начинается с $2a$...) и вставь в BCRYPT_HASH выше
> ```

### 3.3 Проверка и применение

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

Проверка HTTPS:

```bash
curl -s -u admin:ВашПароль https://ДОМЕН_ЗАВОДА/
# Ожидаемый ответ: HTML дашборда
```

---

## 4. Обновление проекта

Когда в код внесены изменения:

**Шаг 1 — залить новые файлы, не трогая данные и секреты** (с Windows-машины):

```powershell
rsync -avz --exclude ".venv" --exclude "data" --exclude ".env" `
  "A:/DevAI/Projects/KontentZavod/" `
  root@mako-play:/srv/KontentZavod/
```

**Шаг 2 — восстановить владельца файлов** (иначе сервис не прочитает новые файлы):

```bash
chown -R kontentzavod:kontentzavod /srv/KontentZavod
```

**Шаг 3 — обновить зависимости, если изменился requirements.txt:**

```bash
sudo -u kontentzavod /srv/KontentZavod/.venv/bin/pip install -r /srv/KontentZavod/requirements.txt
```

**Шаг 4 — перезапустить сервис:**

```bash
systemctl restart kontentzavod
systemctl status kontentzavod
curl -s http://127.0.0.1:8200/
```

> `data/kontentzavod.db`, `data/outbox/` и `.env` rsync не трогает (`--exclude data --exclude .env`).
> SQLite-база при обновлении сохраняется: `init_db()` использует `CREATE TABLE IF NOT EXISTS`.

### Миграции БД (этап 7)

При обновлении до этапа 7 («Завод 2.0») дополнительных действий с БД **не требуется**:
миграции идемпотентны и применяются автоматически при рестарте сервиса через `init_db()`.

Что меняется на лету:

- Новые таблицы `strategies` и `plan_items` — создаются через `CREATE TABLE IF NOT EXISTS`.
- Колонки `projects.stage`, `projects.settings`, `project_platforms.content_types` — добавляются
  через `ALTER TABLE ... ADD COLUMN` (защищено `try/except`, повторный запуск не ломает БД).
- Пересборка таблицы `content` (расширение CHECK-ограничения типов) — безопасная замена
  через временную таблицу; выполняется один раз при первом старте после обновления.

После `systemctl restart kontentzavod` все изменения применяются при первом запросе к БД.

### Новые фоновые джобы (этап 7)

При `ENABLE_SCHEDULER=1` запускаются два дополнительных джоба:

- **process_brain** (каждые 2 минуты) — генерирует стратегию и rolling контент-план.
- **process_factory** (каждую минуту) — генерирует контент для одобренных пунктов плана
  и создаёт записи в расписании.

Если `ENABLE_SCHEDULER=0` — джобы не работают, публикация не происходит.
Для ручного запуска тика без перезапуска — используй консоль Python или `curl` к внутреннему эндпоинту
(появится в следующих волнах).

### Новый воркфлоу 2.0

Начиная с этапа 7, основной сценарий работы изменился:

1. **Проект** — создать через `/projects/new` (с ИИ-генерацией профиля или вручную).
2. **▶ В работу** — на странице проекта настроить площадки, типы контента, горизонт плана
   и нажать «Запустить» (`POST /projects/{slug}/launch`).
3. **Стратегия** — `process_brain` автоматически генерирует маркетинговую стратегию
   (статус `generating` → `active`); просмотр на `/projects/{slug}/strategy`.
4. **`/plan` — одобрение** — шахматка предлагаемых пунктов плана (`proposed`); оператор
   одобряет нужные (`approved`) и включает «⚡ Авто-генерацию» для автоматической фабрики.
5. **Фабрика** — `process_factory` генерирует контент для одобренных пунктов в lookahead-окне
   и создаёт записи в расписании.
6. **`/queue`** — шахматка готового контента: видны статусы генерации и публикации,
   модалка показывает превью и ссылку на outbox.
7. **Автопубликация** — `process_due` публикует по расписанию (dry-run или реальная при
   `PUBLISH_DRY_RUN=0`); для manual-платформ (Яндекс.Дзен, Instagram) — статус
   `manual_pending`, оператор завершает вручную через `/manual`.

> **Старый флоу «идеи → пост → ревью» продолжает работать в полном объёме** — он не удалён
> и доступен на тех же роутах (`/review`, `/ideas`). Оба флоу сосуществуют в одной БД.

---

## 5. Диагностика

### Просмотр логов в реальном времени

```bash
journalctl -u kontentzavod -f
```

### Последние 100 строк логов

```bash
journalctl -u kontentzavod -n 100 --no-pager
```

### Типовые проблемы

**Порт 8200 уже занят:**

```bash
ss -tlnp | grep 8200
# Найди PID и kill, или смени PORT в ExecStart юнита
```

**Нет прав на data/:**

```bash
chown -R kontentzavod:kontentzavod /srv/KontentZavod/data
chmod 750 /srv/KontentZavod/data
systemctl restart kontentzavod
```

**502 Bad Gateway в Caddy — сервис не отвечает:**

```bash
systemctl status kontentzavod          # смотри Active: и последние строки
journalctl -u kontentzavod -n 50 --no-pager   # смотри ошибку запуска
curl -v http://127.0.0.1:8200/         # прямая проверка
```

**FERNET_KEY не задан — приложение упало при старте:**

```bash
journalctl -u kontentzavod -n 20 --no-pager
# RuntimeError: FERNET_KEY не задан в .env
# Исправить:
nano /srv/KontentZavod/.env   # добавить FERNET_KEY=...
systemctl restart kontentzavod
```

**Роутер отвечает 404 при генерации, хотя curl с ключом работает:**

Симптом: генерация падает с ошибкой 404 от роутера, но `curl -s http://127.0.0.1:8100/health`
и прямой запрос с ключом проходят успешно.

Причина: в `.env` есть inline-комментарий в строке с URL или ключом:
```
LLM_ROUTER_BASE_URL=http://127.0.0.1:8100/v1  # этот хвост попадает в SDK
```
python-dotenv такой комментарий отсекает, но если значение попало в окружение
иначе (например, через старый `EnvironmentFile=` в юните), хвост уходит в openai SDK
и строится мусорный URL.

Диагностика — проверить, что реально видит процесс:
```bash
systemd-run --unit test-env --wait -p EnvironmentFile=/srv/KontentZavod/.env \
  bash -c 'env | grep LLM_ROUTER'
journalctl -u test-env --no-pager
```

Решение: убрать inline-комментарии из `.env` — комментарии **только на отдельных строках**.
Приложение читает `.env` через python-dotenv самостоятельно; `EnvironmentFile=` в юните
не используется.

**LLM-Router недоступен — ошибки генерации:**

```bash
curl -s http://127.0.0.1:8100/health
# Если не отвечает:
systemctl status llmrouter
# LLM_ROUTER_BASE_URL в .env должен быть http://127.0.0.1:8100/v1
# (или https://router.mak-o.ru/v1 — оба варианта рабочие)
```

**Планировщик не публикует (ENABLE_SCHEDULER=0):**

```bash
grep ENABLE_SCHEDULER /srv/KontentZavod/.env
# Должно быть ENABLE_SCHEDULER=1 на проде
nano /srv/KontentZavod/.env
systemctl restart kontentzavod
```

**Токены площадок не расшифровываются после смены FERNET_KEY:**

Симптом: `Не удалось расшифровать credentials` в логах; публикации уходят в dry-run.
Решение: зайди в `/projects` → каждый проект → каждая площадка → заново введи токены
(бот-токен TG, токен VK и т.д.) и сохрани.

**Права на .env (чужой пользователь читает секреты):**

```bash
chown kontentzavod:kontentzavod /srv/KontentZavod/.env
chmod 600 /srv/KontentZavod/.env
```

**После rsync новые файлы недоступны сервису:**

```bash
# root залил файлы, kontentzavod их не читает:
chown -R kontentzavod:kontentzavod /srv/KontentZavod
systemctl restart kontentzavod
```

**После добавления токенов площадок нужно включить реальную публикацию:**

```bash
nano /srv/KontentZavod/.env
# Изменить PUBLISH_DRY_RUN=1 → PUBLISH_DRY_RUN=0
systemctl restart kontentzavod
```

---

## 6. Проверка после деплоя (чеклист)

```bash
# 1. Оба сервиса работают
systemctl is-active llmrouter kontentzavod

# 2. Роутер отвечает
curl -s http://127.0.0.1:8100/health

# 3. Завод отвечает
curl -s http://127.0.0.1:8200/

# 4. HTTPS через Caddy (basic_auth)
curl -s -u admin:ВашПароль https://ДОМЕН_ЗАВОДА/

# 5. Аналитика и ручная публикация доступны
curl -s -u admin:ВашПароль https://ДОМЕН_ЗАВОДА/analytics
curl -s -u admin:ВашПароль https://ДОМЕН_ЗАВОДА/manual

# 6. Дашборд доступен (нет 502)
# 7. Зайти в /projects/new — создать тестовый проект
# 8. Сгенерировать идеи (FAKE_LLM=0, нужен LLM_ROUTER_KEY)
# 9. Проверить /calendar и /review
# 10. Джобы планировщика видны в логе:
journalctl -u kontentzavod -n 50 --no-pager | grep -i scheduler
```
