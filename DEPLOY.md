# Деплой КонтентЗавода на VPS (Ubuntu)

Копипаст-инструкция. Все переменные задаются **один раз** в разделе 1 — дальше команды
используют их без правок.

> **Предварительное условие:** LLM-Router уже задеплоен и работает по адресу
> `http://127.0.0.1:8100/v1`. Если нет — сначала выполни `LLMRouter/DEPLOY.md`.

---

## 1. Переменные окружения деплоя

Выполни эти строки в начале каждой SSH-сессии, где работаешь с заводом:

```bash
DOMAIN=content.example.com          # поддомен, который укажешь в DNS
DEPLOY_DIR=/opt/kontentzavod        # рабочая директория на сервере
UNIT=kontentzavod                   # имя systemd-юнита (kontentzavod.service)
APP_USER=kontentzavod               # системный пользователь для сервиса
```

---

## 2. Первичный деплой

### 2.1 Проверка Python и создание системного пользователя

```bash
python3 --version          # нужен 3.10+; если ниже — см. примечание
id $APP_USER 2>/dev/null || sudo useradd --system --no-create-home --shell /usr/sbin/nologin $APP_USER
```

> Если Python < 3.10: `sudo apt update && sudo apt install -y python3.10 python3.10-venv python3.10-distutils`

> **Пользователь:** используем отдельного пользователя `kontentzavod` — независимо от `llmrouter`.
> Это изолирует права доступа к файлам данных каждого сервиса.

### 2.2 Создание директории и папок данных

```bash
sudo mkdir -p $DEPLOY_DIR/data/outbox
sudo chown -R $APP_USER:$APP_USER $DEPLOY_DIR
```

### 2.3 Заливка файлов

**Вариант A — rsync с Windows-машины** (выполняется локально в PowerShell/cmd):

```powershell
# Замени user@your-vps на реальные данные
rsync -avz --exclude ".venv" --exclude "data" --exclude ".env" `
  "A:/DevAI/Projects/KontentZavod/" `
  user@your-vps:/opt/kontentzavod/
```

> Для scp (без исключений — медленнее):
> ```powershell
> scp -r "A:\DevAI\Projects\KontentZavod\app" user@your-vps:/opt/kontentzavod/
> scp "A:\DevAI\Projects\KontentZavod\requirements.txt" user@your-vps:/opt/kontentzavod/
> scp "A:\DevAI\Projects\KontentZavod\.env.example" user@your-vps:/opt/kontentzavod/
> ```

**Вариант B — git clone** (когда репозиторий появится):

```bash
sudo -u $APP_USER git clone https://github.com/ТВОЙ_REPO/kontentzavod.git $DEPLOY_DIR
```

### 2.4 Создание venv и установка зависимостей

```bash
sudo -u $APP_USER python3 -m venv $DEPLOY_DIR/.venv
sudo -u $APP_USER $DEPLOY_DIR/.venv/bin/pip install --upgrade pip
sudo -u $APP_USER $DEPLOY_DIR/.venv/bin/pip install -r $DEPLOY_DIR/requirements.txt
```

> **ffmpeg** должен быть установлен на сервере (для этапа 3 — видеоконвейер):
> ```bash
> sudo apt install -y ffmpeg
> ffmpeg -version   # проверка
> ```

### 2.5 Создание .env на сервере

```bash
sudo tee $DEPLOY_DIR/.env > /dev/null << 'EOF'
# Путь к SQLite-базе данных
DB_PATH=data/kontentzavod.db

# Директория данных (outbox, видео, кэш)
DATA_DIR=data

# LLM-Router: на сервере роутер крутится локально
LLM_ROUTER_BASE_URL=http://127.0.0.1:8100/v1

# Виртуальный ключ роутера — получить в /admin/keys после деплоя роутера
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
EOF
sudo chown $APP_USER:$APP_USER $DEPLOY_DIR/.env
sudo chmod 600 $DEPLOY_DIR/.env
```

Заполни значения:

```bash
# Сгенерировать новый FERNET_KEY:
sudo -u $APP_USER $DEPLOY_DIR/.venv/bin/python3 \
  -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Скопируй вывод и вставь в .env:
sudo nano $DEPLOY_DIR/.env
```

**Важно после первого деплоя:**

- Сгенерируй новый `FERNET_KEY` командой выше — не оставляй тестовый.
- ⚠️ **FERNET_KEY менять нельзя после добавления токенов площадок!** Если сменишь ключ,
  все сохранённые токены TG/VK/etc не расшифруются — придётся заново ввести их через
  дашборд `/projects` → секция «Площадки» для каждого проекта.
- Получи `LLM_ROUTER_KEY` в кабинете роутера (http://127.0.0.1:8100/admin → Ключи → Выпустить).
- `PUBLISH_DRY_RUN=1` оставь до момента, когда введёшь реальные токены в дашборде.
  После ввода токенов переключи на `0` и перезапусти сервис.

---

## 3. systemd unit

```bash
sudo tee /etc/systemd/system/$UNIT.service > /dev/null << EOF
[Unit]
Description=КонтентЗавод — генерация и публикация контента
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$DEPLOY_DIR
EnvironmentFile=$DEPLOY_DIR/.env
ExecStart=$DEPLOY_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8200 --workers 1
Restart=always
RestartSec=5

# Ограничение памяти: при 4GB RAM резервируем не более 768MB под завод.
# На этапе 3 (видеоконвейер, рендер ffmpeg) лимит нужно поднять до 1.5-2GB:
#   MemoryMax=1536M
# До тех пор держим умеренно — роутер занимает 512MB, остаток для ОС.
MemoryMax=768M

# Безопасность
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=$DEPLOY_DIR/data

[Install]
WantedBy=multi-user.target
EOF
```

Активация и запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now $UNIT
```

Проверка:

```bash
sudo systemctl status $UNIT
curl -s http://127.0.0.1:8200/
# Ожидаемый ответ: HTML страница дашборда
```

---

## 4. Caddy — добавление поддомена

> **Не заменяй существующий Caddyfile!** Только добавь блок ниже.

### 4.1 DNS

Добавь A-запись у своего регистратора:

```
content.example.com  →  IP_ТВОЕГО_VPS
```

Убедись, что запись применилась: `nslookup $DOMAIN`

### 4.2 Добавление блока в Caddyfile

Открой существующий Caddyfile:

```bash
sudo nano /etc/caddy/Caddyfile
```

Добавь в **конец файла**:

```caddyfile
content.example.com {
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

> **Генерация bcrypt-хэша для Caddy:**
> ```bash
> caddy hash-password --plaintext "ВашПароль"
> # Скопируй вывод (начинается с $2a$...) и вставь в BCRYPT_HASH выше
> ```

> Или используй переменную для поддомена:
> ```bash
> # ВАЖНО: сначала задай DOMAIN (раздел 1), затем:
> echo "
> $DOMAIN {
>     basic_auth {
>         admin BCRYPT_HASH
>     }
>     reverse_proxy 127.0.0.1:8200
> }" | sudo tee -a /etc/caddy/Caddyfile
> ```

### 4.3 Проверка и применение

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Проверка HTTPS:

```bash
curl -s -u admin:ВашПароль https://$DOMAIN/
# Ожидаемый ответ: HTML дашборда
```

---

## 5. Обновление проекта

Когда в код внесены изменения:

**Шаг 1 — залить новые файлы, не трогая данные и секреты** (с Windows-машины):

```powershell
rsync -avz --exclude ".venv" --exclude "data" --exclude ".env" `
  "A:/DevAI/Projects/KontentZavod/" `
  user@your-vps:/opt/kontentzavod/
```

**Шаг 2 — обновить зависимости, если изменился requirements.txt:**

```bash
sudo -u $APP_USER $DEPLOY_DIR/.venv/bin/pip install -r $DEPLOY_DIR/requirements.txt
```

**Шаг 3 — перезапустить сервис:**

```bash
sudo systemctl restart $UNIT
sudo systemctl status $UNIT
curl -s http://127.0.0.1:8200/
```

> `data/kontentzavod.db`, `data/outbox/` и `.env` rsync не трогает (`--exclude data --exclude .env`).
> SQLite-база при обновлении сохраняется: `init_db()` использует `CREATE TABLE IF NOT EXISTS`.

---

## 6. Диагностика

### Просмотр логов в реальном времени

```bash
sudo journalctl -u $UNIT -f
```

### Последние 100 строк логов

```bash
sudo journalctl -u $UNIT -n 100 --no-pager
```

### Типовые проблемы

**Порт 8200 уже занят:**

```bash
sudo ss -tlnp | grep 8200
# Найди PID и kill, или смени PORT в ExecStart юнита
```

**Нет прав на data/:**

```bash
sudo chown -R $APP_USER:$APP_USER $DEPLOY_DIR/data
sudo chmod 750 $DEPLOY_DIR/data
sudo systemctl restart $UNIT
```

**502 Bad Gateway в Caddy — сервис не отвечает:**

```bash
sudo systemctl status $UNIT          # смотри Active: и последние строки
sudo journalctl -u $UNIT -n 50 --no-pager   # смотри ошибку запуска
curl -v http://127.0.0.1:8200/        # прямая проверка
```

**FERNET_KEY не задан — приложение упало при старте:**

```bash
sudo journalctl -u $UNIT -n 20 --no-pager
# RuntimeError: FERNET_KEY не задан в .env
# Исправить: sudo nano /opt/kontentzavod/.env → добавить FERNET_KEY=...
sudo systemctl restart $UNIT
```

**LLM-Router недоступен — ошибки генерации:**

```bash
curl -s http://127.0.0.1:8100/health
# Если не отвечает: sudo systemctl status llmrouter
# LLM_ROUTER_BASE_URL в .env должен быть http://127.0.0.1:8100/v1
```

**Планировщик не публикует (ENABLE_SCHEDULER=0):**

```bash
grep ENABLE_SCHEDULER $DEPLOY_DIR/.env
# Должно быть ENABLE_SCHEDULER=1 на проде
sudo nano $DEPLOY_DIR/.env
sudo systemctl restart $UNIT
```

**Токены площадок не расшифровываются после смены FERNET_KEY:**

Симптом: `Не удалось расшифровать credentials` в логах; публикации уходят в dry-run.
Решение: зайди в `/projects` → каждый проект → каждая площадка → заново введи токены
(бот-токен TG, токен VK и т.д.) и сохрани.

**Права на .env (чужой пользователь читает секреты):**

```bash
sudo chown $APP_USER:$APP_USER $DEPLOY_DIR/.env
sudo chmod 600 $DEPLOY_DIR/.env
```

**После добавления токенов площадок нужно включить реальную публикацию:**

```bash
sudo nano $DEPLOY_DIR/.env
# Изменить PUBLISH_DRY_RUN=1 → PUBLISH_DRY_RUN=0
sudo systemctl restart $UNIT
```

---

## 7. Проверка после деплоя (чеклист)

```bash
# 1. Оба сервиса работают
sudo systemctl is-active llmrouter kontentzavod

# 2. Роутер отвечает
curl -s http://127.0.0.1:8100/health

# 3. Завод отвечает
curl -s http://127.0.0.1:8200/

# 4. HTTPS через Caddy (basic_auth)
curl -s -u admin:ВашПароль https://content.example.com/

# 5. Дашборд доступен (нет 502)
# 6. Зайти в /projects/new — создать тестовый проект
# 7. Сгенерировать идеи (FAKE_LLM=0, нужен LLM_ROUTER_KEY)
# 8. Проверить /calendar и /review
```
