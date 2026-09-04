import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.api.v1.router import api_router
from app.api.v1.endpoints import admin
from app.db.session import engine, Base

logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "https://pifagor.by,https://www.pifagor.by,https://backend-pifagor.onrender.com,http://localhost:5173,http://localhost:5174,http://localhost:8000",
    ).split(",")
    if origin.strip()
]

ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "").strip() in {"1", "true", "True", "yes", "YES"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("X-XSS-Protection", "0")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    _hits: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
    _limits = {
        ("POST", "/api/v1/auth/login"): (20, 60),
        ("POST", "/api/v1/auth/register"): (10, 300),
        ("POST", "/api/v1/auth/refresh"): (60, 60),
        ("GET", "/api/v1/auth/invite-codes/validate"): (30, 60),
        ("POST", "/api/v1/requests"): (15, 300),
    }

    async def dispatch(self, request: Request, call_next):
        key = (request.method, request.url.path)
        limit = self._limits.get(key)
        if limit:
            max_hits, window_seconds = limit
            client_ip = request.headers.get(
                "x-forwarded-for",
                request.client.host if request.client else "unknown",
            ).split(",")[0].strip()
            bucket_key = (request.method, request.url.path, client_ip)
            now = time.monotonic()
            hits = self._hits[bucket_key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= max_hits:
                return JSONResponse(
                    {"detail": "Слишком много запросов, попробуйте позже"},
                    status_code=429,
                )
            hits.append(now)
        return await call_next(request)


async def _daily_email_task():
    from app.services.email_parser import run_email_parse
    from app.db.session import async_session_maker

    while True:
        try:
            async with async_session_maker() as db:
                await run_email_parse(db)
        except Exception as e:
            logger.error("Daily email parse failed: %s", e)
        await asyncio.sleep(86400)


async def _init_database_schema() -> None:
    """Best-effort schema bootstrap. Never crash the process on transient DB issues."""
    logger.info("Starting database schema initialization")
    try:
        async with asyncio.timeout(120):
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

                for sql in (
                    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lessonstatus') THEN ALTER TYPE lessonstatus ADD VALUE IF NOT EXISTS 'trial'; END IF; END $$",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS lesson_price DOUBLE PRECISION NOT NULL DEFAULT 40",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS crm_status VARCHAR(50) NOT NULL DEFAULT 'Пробное'",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS lessons_per_week INTEGER",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS notes TEXT",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS channel VARCHAR(100)",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS subjects_text TEXT",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS tutors_text TEXT",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS contract_label VARCHAR(100)",
                    "ALTER TABLE tutor_contracts ADD COLUMN IF NOT EXISTS signed_file_url VARCHAR(500)",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS lesson_id INTEGER REFERENCES lessons(id)",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS material_score INTEGER",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS material_comment TEXT",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS successes TEXT",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS difficulties TEXT",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS homework_status VARCHAR(120)",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS homework_comment TEXT",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS engagement_score INTEGER",
                    "UPDATE child_profiles SET crm_status = 'Пробное' WHERE crm_status LIKE 'Р%' OR crm_status IS NULL",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_tutor_id ON lessons (tutor_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_child_id ON lessons (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_subject_id ON lessons (subject_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_date ON lessons (date)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_status ON lessons (status)",
                    "CREATE INDEX IF NOT EXISTS ix_users_first_name ON users (first_name)",
                    "CREATE INDEX IF NOT EXISTS ix_users_last_name ON users (last_name)",
                ):
                    try:
                        await conn.execute(text(sql))
                    except Exception:
                        logger.exception("Schema statement failed (continuing): %s", sql[:120])

                for name, slug in (
                    ("Математика", "matematika"),
                    ("Физика", "fizika"),
                    ("Английский язык", "angliyskiy"),
                    ("Русский язык", "russkiy"),
                    ("Белорусский язык", "belorusskiy"),
                    ("Биология", "biologiya"),
                    ("Химия", "himiya"),
                ):
                    try:
                        await conn.execute(
                            text(
                                "INSERT INTO subjects (name, slug, is_active) "
                                "VALUES (:name, :slug, TRUE) "
                                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, is_active = TRUE"
                            ),
                            {"name": name, "slug": slug},
                        )
                    except Exception:
                        logger.exception("Subject seed failed for %s", slug)

        logger.info("Database schema initialization complete")
    except Exception:
        # Render free tier / cold DB can time out; keep the web process alive.
        logger.exception(
            "Database schema initialization failed; service will keep running and retry via requests"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _init_database_schema()
    task = asyncio.create_task(_daily_email_task())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Пифагор API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
)

app.include_router(api_router)
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "pifagor-api"}


@app.get("/_maint/debug-student-q8w3n5")
async def _maint_debug_student(name: str):
    """Разовая диагностическая ссылка (только чтение, ничего не меняет).
    Показывает по ученику: когда его профиль был создан, все его занятия
    и все чеки, которые на него сматчены. Удалить после использования."""
    from sqlalchemy import select as _select
    from sqlalchemy.orm import joinedload as _joinedload
    from app.db.session import AsyncSessionLocal
    from app.models.models import ChildProfile, User, Lesson, EmailReceipt

    pattern = f"%{name}%"
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            _select(ChildProfile)
            .join(User, ChildProfile.user_id == User.id)
            .options(_joinedload(ChildProfile.user))
            .where((User.first_name.ilike(pattern)) | (User.last_name.ilike(pattern)))
        )
        children = result.unique().scalars().all()
        if not children:
            return {"error": f"Ученик по имени '{name}' не найден"}

        output = []
        for child in children:
            lessons_res = await db.execute(
                _select(Lesson).where(Lesson.child_id == child.id).order_by(Lesson.date)
            )
            lessons = [
                {"date": str(l.date), "status": l.status, "is_free_trial": l.is_free_trial}
                for l in lessons_res.scalars().all()
            ]

            receipts_res = await db.execute(
                _select(EmailReceipt).where(EmailReceipt.child_id == child.id).order_by(EmailReceipt.payment_date)
            )
            receipts = [
                {
                    "payment_date": str(r.payment_date),
                    "created_at": str(r.created_at),
                    "amount": r.amount,
                    "payer_name": r.payer_name,
                    "receipt_number": r.receipt_number,
                }
                for r in receipts_res.scalars().all()
            ]

            output.append({
                "child_id": child.id,
                "name": f"{child.user.last_name} {child.user.first_name}",
                "user_created_at": str(child.user.created_at),
                "lesson_price": child.lesson_price,
                "lessons_count": len(lessons),
                "lessons": lessons,
                "receipts_count": len(receipts),
                "receipts": receipts,
            })

        return output


current_dir = os.path.dirname(os.path.abspath(__file__))

uploads_dir = os.path.join(current_dir, "uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

static_dir = os.path.join(current_dir, "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
