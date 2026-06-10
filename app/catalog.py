"""
Каталог типов контента per платформа.

Только корректные для каждой платформы типы — контракт этапа 7.
Атрибуты типа:
  label      — человекочитаемое название (русский)
  kind       — text | video | story
  publish    — auto | manual
  default_on — включён по умолчанию (bool)
"""

CONTENT_TYPES: dict[str, dict[str, dict]] = {
    "telegram": {
        "post":  {"label": "Пост",           "kind": "text",  "publish": "auto",   "default_on": True},
        "video": {"label": "Видео",           "kind": "video", "publish": "auto",   "default_on": True},
    },
    "vk": {
        "post":  {"label": "Пост",            "kind": "text",  "publish": "auto",   "default_on": True},
        "video": {"label": "Видео (в стену)", "kind": "video", "publish": "auto",   "default_on": True},
        "story": {"label": "История",         "kind": "story", "publish": "manual", "default_on": False},
    },
    "instagram": {
        "post":  {"label": "Пост",            "kind": "text",  "publish": "manual", "default_on": True},
        "story": {"label": "История",         "kind": "story", "publish": "manual", "default_on": True},
        "reel":  {"label": "Reels",           "kind": "video", "publish": "manual", "default_on": True},
    },
    "youtube": {
        "short": {"label": "Shorts",          "kind": "video", "publish": "auto",   "default_on": True},
    },
    "dzen": {
        "post":    {"label": "Пост",          "kind": "text",  "publish": "manual", "default_on": True},
        "article": {"label": "Статья",        "kind": "text",  "publish": "manual", "default_on": False},
    },
}


def allowed_types(platform: str) -> dict[str, dict]:
    """
    Вернуть все допустимые типы контента для платформы.

    Args:
        platform: имя платформы (telegram, vk, instagram, youtube, dzen)

    Returns:
        Словарь {type_key: {label, kind, publish, default_on}}
        или пустой dict, если платформа не найдена.
    """
    return CONTENT_TYPES.get(platform, {})


def default_types(platform: str) -> dict[str, bool]:
    """
    Вернуть словарь {type_key: default_on} для платформы.

    Удобно для заполнения project_platforms.content_types при создании платформы.

    Args:
        platform: имя платформы

    Returns:
        Словарь {type_key: True|False} — только типы платформы.
        Пустой dict, если платформа не найдена.
    """
    return {k: v["default_on"] for k, v in CONTENT_TYPES.get(platform, {}).items()}


def type_info(platform: str, ctype: str) -> dict | None:
    """
    Вернуть описание типа контента или None, если не существует.

    Args:
        platform: имя платформы
        ctype:    ключ типа (post, video, story, reel, short, article)

    Returns:
        Словарь {label, kind, publish, default_on} или None.
    """
    return CONTENT_TYPES.get(platform, {}).get(ctype)


def is_valid(platform: str, ctype: str) -> bool:
    """
    Проверить, допустим ли тип контента для платформы.

    Args:
        platform: имя платформы
        ctype:    ключ типа

    Returns:
        True если тип существует для данной платформы, иначе False.
    """
    return ctype in CONTENT_TYPES.get(platform, {})
