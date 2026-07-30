import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.core.deps import get_current_user
from app.models.models import User

router = APIRouter(prefix="/files", tags=["files"])

UPLOAD_DIR = Path(__file__).resolve().parents[3] / "uploads"
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".webp"}
MAX_FILE_SIZE = 15 * 1024 * 1024


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a cabinet document or image and return its public URL."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Разрешены PDF, DOC, DOCX, JPG, PNG и WEBP")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Файл слишком большой, максимум 15 МБ")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / stored_name).write_bytes(contents)

    return {"file_url": f"/uploads/{stored_name}", "file_name": file.filename}
