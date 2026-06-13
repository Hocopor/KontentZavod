"""
Клиент LLM-Router для КонтентЗавода.

Все LLM-вызовы идут ТОЛЬКО через LLM-Router (openai SDK, base_url из .env).
Никаких прямых обращений к DeepSeek/Gemini/etc.

────────────────────────────────────────────────────────────
FAKE_LLM=1 (для тестов и локальной разработки без сети):

  purpose='ideas'  → JSON-массив из 5 объектов:
      [
        {"text": "Идея 1: <тема>"},
        ...
      ]

  purpose='script' → JSON с текстами per-платформа:
      {
        "title":    "Заголовок поста",
        "telegram": {
            "text":    "Полный текст поста для Telegram (до 4096 символов)",
            "hashtags": ["#тег1", "#тег2"]
        },
        "vk": {
            "text":    "Текст для VK (до 15000 символов)",
            "hashtags": ["#тег1", "#тег2"]
        },
        "youtube": {
            "title":       "Название Shorts-видео",
            "description": "Описание для YouTube",
            "hashtags":    ["#тег1", "#тег2"]
        },
        "instagram": {
            "caption":  "Подпись для Instagram (до 2200 символов)",
            "hashtags": ["#тег1", "#тег2"]
        },
        "dzen": {
            "title": "Заголовок статьи Дзен",
            "text":  "Текст статьи (поддерживает Markdown)"
        },
        "features": {
            "hook_type":  "вопрос|факт|история|провокация",
            "topic":      "основная тема поста",
            "length":     "short|medium|long",
            "format":     "text|carousel|reel",
            "ab_variant": "A|B|null"
        }
      }

  purpose='video_script' → JSON с video-сценарием:
      {
        "title": "Заголовок видео",
        "video": {
            "voice":  "dmitry",
            "mood":   "energetic",
            "scenes": [
                {"text": "Фраза сцены 1", "keywords": ["english", "keywords"]},
                ...
            ]
        },
        "telegram":  {...},
        "features":  {"hook_type": "факт", "topic": "...", "length": "short",
                      "format": "reel", "ab_variant": "null"}
      }

  purpose='analyze' → JSON с инсайтами аналитики:
      {
        "insights": [
            {"insight": "Краткая применимая формулировка", "weight": 1.5},
            ...
        ],
        "deactivate": ["текст устаревшего learning", ...]
      }

  purpose='profile' → JSON полей проекта (audience, tone, cta, themes, forbidden, extra)

  purpose='strategy' → JSON маркетинговой стратегии (summary, positioning,
      platforms: все 5 платформ с goals, rubrics[], content_mix, best_times[], kpi)

  purpose='strategy_revise' → то же, что strategy + ключ changes_summary

  purpose='plan' → JSON контент-плана: {items: [{date, time, content_type,
      title, brief: {hook, outline, cta, keywords[], rubric}}], 5-6 штук}

  purpose='item_post' → JSON поста для одной платформы:
      {title, text, hashtags[], features: {hook_type, topic, length, format, ab_variant}}

  purpose='item_story' → JSON истории:
      {title, image_prompt (en), overlay_text, caption,
       features: {hook_type, topic, length, format, ab_variant}}

  purpose='directives_parse' → JSON с массивом директив:
      {"directives": [
        {"text": "...", "scope": "plan",
         "parsed": {"platform": "telegram", "content_type": "post", "per_week": 7}},
        {"text": "...", "scope": "strategy", "parsed": null}
      ]}

  purpose='strategy_check' → JSON результата проверки стратегии:
      {"ok": true, "violations": []}

  purpose=None / прочие → строка 'FAKE_LLM response'

────────────────────────────────────────────────────────────
Структура texts JSON в таблице content (для следующих волн):

  Поле content.texts хранит тот же объект, что purpose='script' возвращает
  в ключах "telegram", "vk", "youtube", "instagram", "dzen".
  Поле content.features — JSON из ключа "features" того же объекта.

  Пример доступа в pipeline:
      import json
      texts = json.loads(row["texts"])
      tg_text = texts["telegram"]["text"]
      features = json.loads(row["features"])
      hook = features["hook_type"]
────────────────────────────────────────────────────────────
"""
import json
import logging
from typing import Literal

from openai import OpenAI, APIConnectionError, APIStatusError

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Исключения ───────────────────────────────────────────────────────────────


class LLMError(Exception):
    """Ошибка вызова LLM-Router (сеть, таймаут, 4xx/5xx от роутера)."""


# ─── Заглушки для FAKE_LLM=1 ─────────────────────────────────────────────────

_FAKE_IDEAS = json.dumps(
    [
        {"text": "Идея 1: Как наш продукт решает главную боль клиента за 5 минут"},
        {"text": "Идея 2: 3 мифа о нашей нише, которые мешают людям достичь результата"},
        {"text": "Идея 3: История клиента: с нуля до результата за 30 дней"},
        {"text": "Идея 4: Закулисье: как мы готовим продукт/услугу"},
        {"text": "Идея 5: Сравнение: наш подход vs стандартный рынок"},
    ],
    ensure_ascii=False,
)

_FAKE_SCRIPT = json.dumps(
    {
        "title": "Как мы решаем главную боль клиента",
        "telegram": {
            "text": (
                "🔥 Знаете, что мешает большинству людей получить результат?\n\n"
                "Они ищут сложные решения там, где нужно простое.\n\n"
                "Мы в [Проект] сделали именно это — убрали всё лишнее "
                "и оставили только то, что работает.\n\n"
                "Попробуйте сами 👇"
            ),
            "hashtags": ["#продукт", "#результат", "#кейс"],
        },
        "vk": {
            "text": (
                "Знаете, что мешает большинству людей получить результат?\n\n"
                "Они ищут сложные решения там, где нужно простое.\n\n"
                "Мы в [Проект] сделали именно это — убрали всё лишнее "
                "и оставили только то, что работает.\n\n"
                "Расскажите в комментариях — с какой болью вы сталкивались чаще всего?"
            ),
            "hashtags": ["#продукт", "#результат"],
        },
        "youtube": {
            "title": "Как мы решаем главную боль клиента за 5 минут",
            "description": (
                "В этом видео рассказываем, как [Проект] помогает решить "
                "главную боль клиентов быстро и просто.\n\n"
                "Подписывайтесь на канал!"
            ),
            "hashtags": ["#shorts", "#продукт"],
        },
        "instagram": {
            "caption": (
                "Главная боль клиента — и как мы её решаем 💡\n\n"
                "Простое решение там, где все ищут сложное.\n\n"
                "Ссылка в профиле 👆"
            ),
            "hashtags": ["#продукт", "#результат", "#бизнес"],
        },
        "dzen": {
            "title": "Как мы решаем главную боль клиента: честный разбор",
            "text": (
                "## Постановка проблемы\n\n"
                "Большинство людей ищут сложные решения там, где нужно простое.\n\n"
                "## Наш подход\n\n"
                "Мы убрали всё лишнее и оставили только то, что работает.\n\n"
                "## Результат\n\n"
                "Клиенты получают результат быстрее и с меньшими усилиями."
            ),
        },
        "features": {
            "hook_type": "вопрос",
            "topic": "боль клиента и решение",
            "length": "medium",
            "format": "text",
            "ab_variant": "null",
        },
    },
    ensure_ascii=False,
)

_FAKE_VIDEO_SCRIPT = json.dumps(
    {
        "title": "Как мы решаем главную боль клиента за 60 секунд",
        "video": {
            "voice": "dmitry",
            "mood": "energetic",
            "scenes": [
                {
                    "text": (
                        "Вы тратите часы на задачу, которую можно решить за минуту? "
                        "Мы знаем, как это исправить."
                    ),
                    "keywords": ["business problem", "frustration", "office work"],
                },
                {
                    "text": (
                        "Наш продукт убирает всё лишнее и оставляет только то, что работает. "
                        "Никакой лишней сложности."
                    ),
                    "keywords": ["simple solution", "product demo", "efficiency"],
                },
                {
                    "text": (
                        "Клиенты получают результат уже в первый день. "
                        "Это не магия — это правильный инструмент."
                    ),
                    "keywords": ["happy customer", "success result", "team work"],
                },
                {
                    "text": (
                        "Попробуйте сами — переходите по ссылке в профиле "
                        "и начните бесплатно прямо сейчас."
                    ),
                    "keywords": ["call to action", "link in bio", "start now"],
                },
            ],
        },
        "telegram": {
            "text": (
                "🔥 60 секунд — и вы поймёте, почему тысячи клиентов выбирают [Проект]\n\n"
                "Смотрите видео и переходите по ссылке 👇"
            ),
            "hashtags": ["#продукт", "#результат", "#видео"],
        },
        "vk": {
            "text": (
                "60 секунд — и вы поймёте, почему тысячи клиентов выбирают [Проект].\n\n"
                "Смотрите видео и пишите в комментариях!"
            ),
            "hashtags": ["#продукт", "#результат"],
        },
        "youtube": {
            "title": "Как мы решаем главную боль клиента за 60 секунд",
            "description": "Смотрите, как [Проект] помогает решить главную боль клиентов быстро.",
            "hashtags": ["#shorts", "#продукт"],
        },
        "instagram": {
            "caption": "60 секунд — и вы всё поймёте про [Проект] 💡\n\nСсылка в профиле 👆",
            "hashtags": ["#продукт", "#результат", "#бизнес"],
        },
        "features": {
            "hook_type": "вопрос",
            "topic": "решение главной боли клиента",
            "length": "short",
            "format": "reel",
            "ab_variant": "null",
        },
    },
    ensure_ascii=False,
)

_FAKE_SCRIPT_AB = json.dumps(
    {
        "title": "Как мы решаем главную боль клиента",
        "features": {
            "topic": "боль клиента и решение",
            "length": "medium",
            "format": "text",
        },
        "variants": {
            "A": {
                "hook_type": "вопрос",
                "telegram": {
                    "text": (
                        "Знаете, что мешает большинству людей получить результат?\n\n"
                        "Они ищут сложные решения там, где нужно простое.\n\n"
                        "Мы в [Проект] сделали именно это — убрали всё лишнее "
                        "и оставили только то, что работает.\n\n"
                        "Попробуйте сами 👇"
                    ),
                    "hashtags": ["#продукт", "#результат", "#кейс"],
                },
                "vk": {
                    "text": (
                        "Знаете, что мешает большинству людей получить результат?\n\n"
                        "Они ищут сложные решения там, где нужно простое.\n\n"
                        "Мы в [Проект] сделали именно это — убрали всё лишнее "
                        "и оставили только то, что работает.\n\n"
                        "Расскажите в комментариях!"
                    ),
                    "hashtags": ["#продукт", "#результат"],
                },
                "youtube": {
                    "title": "Как решить главную боль клиента? [Вопрос-хук]",
                    "description": "Разбираем, как [Проект] решает главную боль клиентов.",
                    "hashtags": ["#shorts", "#продукт"],
                },
                "instagram": {
                    "caption": (
                        "Знаете, что мешает большинству людей получить результат? 💡\n\n"
                        "Простое решение — в профиле 👆"
                    ),
                    "hashtags": ["#продукт", "#результат", "#бизнес"],
                },
                "dzen": {
                    "title": "Знаете, что мешает получить результат? Честный ответ",
                    "text": (
                        "## Постановка проблемы\n\n"
                        "Большинство людей ищут сложные решения там, где нужно простое.\n\n"
                        "## Наш подход\n\n"
                        "Мы убрали всё лишнее и оставили только то, что работает."
                    ),
                },
            },
            "B": {
                "hook_type": "провокация",
                "telegram": {
                    "text": (
                        "Вы платите за сложность, которая вам не нужна.\n\n"
                        "Они ищут сложные решения там, где нужно простое.\n\n"
                        "Мы в [Проект] сделали именно это — убрали всё лишнее "
                        "и оставили только то, что работает.\n\n"
                        "Попробуйте сами 👇"
                    ),
                    "hashtags": ["#продукт", "#результат", "#кейс"],
                },
                "vk": {
                    "text": (
                        "Вы платите за сложность, которая вам не нужна.\n\n"
                        "Они ищут сложные решения там, где нужно простое.\n\n"
                        "Мы в [Проект] сделали именно это — убрали всё лишнее "
                        "и оставили только то, что работает.\n\n"
                        "Расскажите в комментариях!"
                    ),
                    "hashtags": ["#продукт", "#результат"],
                },
                "youtube": {
                    "title": "Вы переплачиваете за сложность — вот как это исправить",
                    "description": "Разбираем, как [Проект] решает главную боль клиентов.",
                    "hashtags": ["#shorts", "#продукт"],
                },
                "instagram": {
                    "caption": (
                        "Вы платите за сложность, которая вам не нужна. 🔥\n\n"
                        "Простое решение — в профиле 👆"
                    ),
                    "hashtags": ["#продукт", "#результат", "#бизнес"],
                },
                "dzen": {
                    "title": "Вы платите за сложность, которая вам не нужна",
                    "text": (
                        "## Постановка проблемы\n\n"
                        "Большинство людей ищут сложные решения там, где нужно простое.\n\n"
                        "## Наш подход\n\n"
                        "Мы убрали всё лишнее и оставили только то, что работает."
                    ),
                },
            },
        },
    },
    ensure_ascii=False,
)

_FAKE_ANALYZE = json.dumps(
    {
        "insights": [
            {
                "insight": "Хук-вопрос в первой строке даёт выше вовлечённость, чем факт.",
                "weight": 1.5,
            },
            {
                "insight": "Короткие посты (short) набирают больше просмотров в Telegram.",
                "weight": 1.2,
            },
        ],
        "deactivate": [],
    },
    ensure_ascii=False,
)

_FAKE_PROFILE = json.dumps(
    {
        "audience": "Предприниматели и маркетологи 25–45 лет, ищут инструменты роста",
        "tone": "Экспертный, но дружелюбный — без жаргона, с конкретными примерами",
        "cta": "Записаться на бесплатную консультацию или скачать чек-лист",
        "themes": "Маркетинг, автоматизация бизнеса, кейсы клиентов, лайфхаки роста",
        "forbidden": "Политика, религия, негатив о конкурентах, обещания без доказательств",
        "extra": "Акцент на практических результатах: цифры, сроки, конкретные шаги",
    },
    ensure_ascii=False,
)

# Стратегия v2 (этап 8.2): фазы + goal_share + mix; rubrics — объекты {name, goal, description};
# content_mix в platforms НЕ задаётся — его материализует код build_strategy из mix первой фазы.
_FAKE_STRATEGY = json.dumps(
    {
        "summary": (
            "Контент-стратегия по фазам: сначала быстрый набор аудитории через пользу и охват, "
            "затем прогрев и дозированный вывод на продажи."
        ),
        "positioning": (
            "Практичный эксперт, который не продаёт воздух — даёт конкретные инструменты "
            "и реальные результаты клиентов."
        ),
        "phases": [
            {
                "n": 1,
                "weeks": 2,
                "name": "Запуск: набор аудитории",
                "objective": "быстро набрать первичную аудиторию",
                "goal_share": {"attract": 70, "retain": 25, "sell": 5},
                "mix": {
                    "telegram": {"post": 4, "video": 2},
                    "vk": {"post": 3, "video": 2},
                    "instagram": {"post": 3, "story": 5, "reel": 3},
                    "youtube": {"short": 3},
                    "dzen": {"post": 2},
                },
                "notes": "без продаж, агрессивная подача, высокая частота",
            },
            {
                "n": 2,
                "weeks": 3,
                "name": "Прогрев и продажи",
                "objective": "удерживать интерес и дозированно продавать",
                "goal_share": {"attract": 40, "retain": 40, "sell": 20},
                "mix": {
                    "telegram": {"post": 3, "video": 2},
                    "vk": {"post": 2, "video": 2},
                    "instagram": {"post": 2, "story": 4, "reel": 3},
                    "youtube": {"short": 2},
                    "dzen": {"post": 1},
                },
                "notes": "появляется продающий контент, тон спокойнее",
            },
        ],
        "platforms": {
            "telegram": {
                "goals": "Удержание аудитории, виральность через репосты, лиды в директ",
                "rubrics": [
                    {"name": "Кейс клиента", "goal": "sell", "description": "реальная история результата"},
                    {"name": "Инструмент недели", "goal": "attract", "description": "разбор полезного сервиса"},
                    {"name": "Закулисье", "goal": "retain", "description": "процесс работы команды"},
                ],
                "best_times": ["09:00", "18:00"],
                "kpi": "Охват ≥2000 на пост, CTR в ссылку ≥3%",
            },
            "vk": {
                "goals": "Охват новой аудитории через алгоритм, трафик на сайт",
                "rubrics": [
                    {"name": "Разбор ошибок", "goal": "attract", "description": "типичные провалы и как их избежать"},
                    {"name": "История успеха", "goal": "sell", "description": "путь клиента к результату"},
                ],
                "best_times": ["10:00", "19:00"],
                "kpi": "Охват ≥1500, лайки+репосты ≥5% от охвата",
            },
            "instagram": {
                "goals": "Визуальный имидж бренда, рост подписной базы, прямые продажи",
                "rubrics": [
                    {"name": "Карточки-советы", "goal": "attract", "description": "полезные карусели"},
                    {"name": "Stories-опрос", "goal": "retain", "description": "вовлечение через вопросы"},
                ],
                "best_times": ["11:00", "20:00"],
                "kpi": "Охват ≥3000, сохранения постов ≥8%",
            },
            "youtube": {
                "goals": "Демонстрация экспертизы, SEO-трафик, подписки",
                "rubrics": [
                    {"name": "Мини-урок", "goal": "attract", "description": "короткий обучающий ролик"},
                    {"name": "Разбор кейса", "goal": "sell", "description": "результат клиента в формате Shorts"},
                ],
                "best_times": ["12:00", "17:00"],
                "kpi": "Просмотры ≥500, удержание ≥50%",
            },
            "dzen": {
                "goals": "SEO-трафик, читатели из поиска Яндекса, монетизация",
                "rubrics": [
                    {"name": "Подробная статья", "goal": "attract", "description": "глубокий разбор темы"},
                    {"name": "Обзор тренда", "goal": "retain", "description": "анализ актуального явления"},
                ],
                "best_times": ["09:00", "15:00"],
                "kpi": "Дочитывания ≥60%, трафик из поиска растёт на 10%/мес",
            },
        },
    },
    ensure_ascii=False,
)

_FAKE_STRATEGY_REVISE = json.dumps(
    {
        "summary": (
            "Обновлённая стратегия с учётом комментария пользователя: "
            "акцент смещён на видеоформаты и короткий развлекательный контент."
        ),
        "positioning": (
            "Эксперт, который объясняет сложное просто — через видео и живые примеры."
        ),
        "phases": [
            {
                "n": 1,
                "weeks": 2,
                "name": "Прогрев на видео",
                "objective": "удерживать интерес через видеоформаты",
                "goal_share": {"attract": 50, "retain": 40, "sell": 10},
                "mix": {
                    "telegram": {"post": 3, "video": 3},
                    "vk": {"post": 2, "video": 3},
                    "instagram": {"post": 2, "story": 5, "reel": 4},
                    "youtube": {"short": 3},
                    "dzen": {"post": 2},
                },
                "notes": "акцент на видео, тон легче",
            },
            {
                "n": 2,
                "weeks": 3,
                "name": "Видео-продажи",
                "objective": "конвертировать прогретую аудиторию",
                "goal_share": {"attract": 35, "retain": 35, "sell": 30},
                "mix": {
                    "telegram": {"post": 2, "video": 3},
                    "vk": {"post": 2, "video": 3},
                    "instagram": {"post": 2, "story": 4, "reel": 4},
                    "youtube": {"short": 2},
                    "dzen": {"post": 1},
                },
                "notes": "продающий контент через живые видео-кейсы",
            },
        ],
        "platforms": {
            "telegram": {
                "goals": "Виральность через видео-контент, рост подписчиков",
                "rubrics": [
                    {"name": "Видео-совет", "goal": "attract", "description": "короткий полезный ролик"},
                    {"name": "Кейс в видео", "goal": "sell", "description": "результат клиента на видео"},
                ],
                "best_times": ["09:00", "19:00"],
                "kpi": "Охват ≥2500 на пост, репосты ≥2% от охвата",
            },
            "vk": {
                "goals": "Видео в рекомендациях VK, новая аудитория",
                "rubrics": [
                    {"name": "Видео-обзор", "goal": "attract", "description": "обзор в формате видео"},
                    {"name": "История клиента", "goal": "sell", "description": "путь клиента"},
                ],
                "best_times": ["10:00", "20:00"],
                "kpi": "Охват видео ≥2000, лайки ≥4%",
            },
            "instagram": {
                "goals": "Reels в рекомендациях, рост новых подписчиков",
                "rubrics": [
                    {"name": "Reels-урок", "goal": "attract", "description": "обучающий Reel"},
                    {"name": "Stories-вопрос", "goal": "retain", "description": "вовлечение в Stories"},
                ],
                "best_times": ["11:00", "21:00"],
                "kpi": "Охват Reels ≥5000, новые подписчики +10%/мес",
            },
            "youtube": {
                "goals": "Рост канала через Shorts, переводить в длинные ролики",
                "rubrics": [
                    {"name": "Shorts-лайфхак", "goal": "attract", "description": "быстрый совет"},
                    {"name": "Shorts-кейс", "goal": "sell", "description": "результат в Shorts"},
                ],
                "best_times": ["12:00", "18:00"],
                "kpi": "Просмотры ≥800, новые подписчики ≥20/мес",
            },
            "dzen": {
                "goals": "Органический трафик через статьи, читатели журнала",
                "rubrics": [
                    {"name": "Экспертная статья", "goal": "attract", "description": "глубокий разбор"},
                    {"name": "Разбор тренда", "goal": "retain", "description": "анализ актуального"},
                ],
                "best_times": ["09:00", "14:00"],
                "kpi": "Дочитывания ≥65%, подписчики журнала +5/мес",
            },
        },
        "changes_summary": (
            "Увеличена доля видео-контента на всех платформах. "
            "Instagram: добавлен дополнительный Reel в неделю. "
            "YouTube: расширены рубрики. Текстовых постов стало меньше."
        ),
    },
    ensure_ascii=False,
)

# Планнер v2: код задаёт слоты [{slot_id, date, weekday, content_type, goal}],
# LLM возвращает тему/бриф на каждый slot_id. FAKE отдаёт 60 элементов (slot_id
# 1..60) — планнер джойнит по slot_id, лишние игнорируются.
_FAKE_PLAN = json.dumps(
    {
        "items": [
            {
                "slot_id": i,
                "title": f"Тестовая тема {i}",
                "brief": {
                    "hook": f"Цепляющий первый экран темы {i}",
                    "outline": f"Структура материала {i}: вступление, суть, вывод",
                    "cta": "Переходите по ссылке" if i % 3 == 0 else "",
                    "keywords": ["тест", "контент", f"тема{i}"],
                    "rubric": "Тестовая рубрика",
                },
            }
            for i in range(1, 61)
        ]
    },
    ensure_ascii=False,
)

_FAKE_ITEM_POST = json.dumps(
    {
        "title": "3 способа увеличить охват поста без рекламного бюджета",
        "text": (
            "Органический охват падает — но есть проверенные способы его вернуть.\n\n"
            "1️⃣ Хук в первых двух строках\n"
            "Первые 2 строки видны без раскрытия поста. Задайте вопрос или "
            "назовите боль — читатель нажмёт «ещё».\n\n"
            "2️⃣ Призыв к действию в комментарии\n"
            "Комментарии буквально поднимают пост в ленте. Спросите мнение, "
            "попросите поделиться своим случаем.\n\n"
            "3️⃣ Публикуйте в «золотое» время\n"
            "Для большинства ниш это 9:00 и 18:00 по рабочим дням. "
            "Проверьте свою аналитику — у каждой аудитории своё время.\n\n"
            "Сохраните пост — пригодится при следующем планировании контента 👇"
        ),
        "hashtags": ["#маркетинг", "#контент", "#smm", "#охват", "#советы"],
        "image_prompt": (
            "social media content creation workspace, laptop with analytics dashboard, "
            "charts showing growth, modern office, warm lighting, "
            "flat lay style, no text on image"
        ),
        "image_keywords": ["social media", "analytics", "growth", "workspace"],
        "features": {
            "hook_type": "вопрос",
            "topic": "органический охват в соцсетях",
            "length": "medium",
            "format": "text",
            "ab_variant": "null",
        },
    },
    ensure_ascii=False,
)

_FAKE_DIRECTIVES_PARSE = json.dumps(
    {
        "directives": [
            {
                "text": "Публиковать по 7 постов в неделю в Telegram",
                "scope": "plan",
                "parsed": {"platform": "telegram", "content_type": "post", "per_week": 7},
            },
            {
                "text": "Меньше продаж в контенте",
                "scope": "strategy",
                "parsed": None,
            },
        ]
    },
    ensure_ascii=False,
)

_FAKE_STRATEGY_CHECK = json.dumps(
    {"ok": True, "violations": []},
    ensure_ascii=False,
)

_FAKE_CENSOR = json.dumps({"verdict": "ok", "reason": ""}, ensure_ascii=False)

_FAKE_ITEM_STORY = json.dumps(
    {
        "title": "Факт дня: органический охват вырос на 40% с одним изменением",
        "image_prompt": (
            "modern digital marketing dashboard with growing chart, "
            "blue and green colors, clean minimal design, vertical format"
        ),
        "image_keywords": ["digital marketing", "analytics", "growth"],
        "overlay_text": "Охват +40% без бюджета",
        "story_type": "carousel",
        "slides": [
            {"text": "Охват +40%", "image_keywords": ["digital marketing", "analytics"], "position": "center"},
            {"text": "Без бюджета", "image_keywords": ["growth", "chart"], "position": "bottom"},
        ],
        "caption": (
            "Один простой хак поднял охват на 40% 📈\n"
            "Подробнее — в следующем посте 👉"
        ),
        "features": {
            "hook_type": "факт",
            "topic": "рост охвата в соцсетях",
            "length": "short",
            "format": "story",
            "ab_variant": "null",
        },
    },
    ensure_ascii=False,
)

# ─── Основной клиент ──────────────────────────────────────────────────────────


def chat(
    messages: list[dict],
    model: str = "auto",
    purpose: str | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """
    Отправить сообщения в LLM-Router и получить текстовый ответ.

    Args:
        messages:   список {"role": "user"|"system"|"assistant", "content": "..."}
        model:      "auto" (роутер выбирает) | "deepseek/deepseek-chat" | ...
        purpose:    назначение вызова ('ideas', 'script', ...) — для логирования
                    и выбора заглушки при FAKE_LLM=1
        json_mode:  если True — передаёт response_format=json_object роутеру
        max_tokens: максимум токенов в ответе (None = дефолт модели)

    Returns:
        Строка с ответом модели.

    Raises:
        LLMError: при сетевых ошибках, таймаутах, 4xx/5xx от роутера.
    """
    # ── Режим заглушки ─────────────────────────────────────────────────────────
    if settings.FAKE_LLM:
        logger.debug("FAKE_LLM=1, purpose=%s — возвращаем заглушку", purpose)
        if purpose == "ideas":
            return _FAKE_IDEAS
        if purpose == "script":
            return _FAKE_SCRIPT
        if purpose == "script_ab":
            return _FAKE_SCRIPT_AB
        if purpose == "video_script":
            return _FAKE_VIDEO_SCRIPT
        if purpose == "analyze":
            return _FAKE_ANALYZE
        if purpose == "profile":
            return _FAKE_PROFILE
        if purpose == "strategy":
            return _FAKE_STRATEGY
        if purpose == "strategy_revise":
            return _FAKE_STRATEGY_REVISE
        if purpose == "plan":
            return _FAKE_PLAN
        if purpose == "item_post":
            return _FAKE_ITEM_POST
        if purpose == "item_story":
            return _FAKE_ITEM_STORY
        if purpose == "directives_parse":
            return _FAKE_DIRECTIVES_PARSE
        if purpose == "strategy_check":
            return _FAKE_STRATEGY_CHECK
        if purpose == "censor":
            return _FAKE_CENSOR
        return "FAKE_LLM response"

    # ── Реальный вызов ─────────────────────────────────────────────────────────
    if not settings.LLM_ROUTER_KEY:
        raise LLMError("LLM_ROUTER_KEY не задан в .env")

    client = OpenAI(
        base_url=settings.LLM_ROUTER_BASE_URL,
        api_key=settings.LLM_ROUTER_KEY,
    )

    kwargs: dict = {
        "model": model,
        "messages": messages,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        logger.info("LLM call: purpose=%s model=%s", purpose, model)
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""
    except APIConnectionError as exc:
        raise LLMError(
            f"Не удалось подключиться к LLM-Router ({settings.LLM_ROUTER_BASE_URL}): {exc}"
        ) from exc
    except APIStatusError as exc:
        raise LLMError(
            f"LLM-Router вернул ошибку {exc.status_code}: {exc.message}"
        ) from exc
    except Exception as exc:
        raise LLMError(f"Неожиданная ошибка LLM: {exc}") from exc
