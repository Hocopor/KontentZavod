"""
Раздача финальных медиафайлов.

Маршруты:
    GET /files/videos/{content_id}.mp4          — финальное видео
    GET /files/videos/{content_id}.jpg          — превью-постер
    GET /files/images/{content_id}.jpg          — первый слайд истории (story)
    GET /files/stories/{content_id}/{idx}.jpg   — слайд истории №idx (1-based)

Видео хранятся в {DATA_DIR}/videos/{content_id}.mp4 / .jpg,
слайды сторис — в {DATA_DIR}/media/{content_id}/slide_N.jpg (волна 8.5A).
Легаси story-картинка — {DATA_DIR}/media/{content_id}/story.jpg (до волны 8.5A).
Возвращает 404 если файл не найден.

Примечание: produce.py сохраняет пути абсолютно в content.files,
но сами файлы рендер кладёт в {DATA_DIR}/videos/{content_id}.mp4 / .jpg.
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from app.config import settings

router = APIRouter(prefix="/files")


@router.get("/videos/{content_id}.mp4")
async def serve_video(content_id: int) -> Response:
    """Отдать финальное mp4 для content_id."""
    path = settings.data_dir_absolute / "videos" / f"{content_id}.mp4"
    if not path.exists():
        return Response(status_code=404, content="Видео не найдено")
    return FileResponse(str(path), media_type="video/mp4")


@router.get("/videos/{content_id}.jpg")
async def serve_preview(content_id: int) -> Response:
    """Отдать превью-постер jpg для content_id."""
    path = settings.data_dir_absolute / "videos" / f"{content_id}.jpg"
    if not path.exists():
        return Response(status_code=404, content="Превью не найдено")
    return FileResponse(str(path), media_type="image/jpeg")


@router.get("/stories/{content_id}/{idx}.jpg")
async def serve_story_slide(content_id: int, idx: int) -> Response:
    """Отдать слайд истории №idx (1-based) для content_id."""
    path = settings.data_dir_absolute / "media" / str(content_id) / f"slide_{idx}.jpg"
    if not path.exists():
        return Response(status_code=404, content="Слайд не найден")
    return FileResponse(str(path), media_type="image/jpeg")


@router.get("/images/{content_id}.jpg")
async def serve_story_image(content_id: int) -> Response:
    """Отдать первый слайд истории (или легаси story.jpg) для content_id."""
    media = settings.data_dir_absolute / "media" / str(content_id)
    for name in ("slide_1.jpg", "post.jpg", "story.jpg"):
        path = media / name
        if path.exists():
            return FileResponse(str(path), media_type="image/jpeg")
    return Response(status_code=404, content="Картинка не найдена")
