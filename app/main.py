import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1.router import api_router
from app.api.v1.endpoints import admin
from app.db.session import engine, Base

logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "https://pifagor.by,https://www.pifagor.by,http://localhost:5173,http://localhost:5174",
    ).split(",")
    if origin.strip()
]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    _hits: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
    _limits = {
        ("POST", "/api/v1/auth/login"): (20, 60),
        ("POST", "/api/v1/auth/register"): (10, 300),
        ("GET", "/api/v1/auth/invite-codes/validate"): (30, 60),
        ("POST", "/api/v1/requests"): (15, 300),
    }

    async def dispatch(self, request: Request, call_next):
        key = (request.method, request.url.path)
        limit = self._limits.get(key)
        if limit:
            max_hits, window_seconds = limit
            client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
            bucket_key = (request.method, request.url.path, client_ip)
            now = time.monotonic()
            hits = self._hits[bucket_key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= max_hits:
                return JSONResponse({"detail": "Слишком много запросов, попробуйте позже"}, status_code=429)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting database schema initialization")
    try:
        async with asyncio.timeout(30):
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
                    "DELETE FROM invite_codes WHERE description IS NOT NULL AND (length(description) > 120 OR description !~ '^[A-Za-zА-Яа-яЁёЎўІіЇїЄє0-9 .''-]{0,120}$')",
                    "UPDATE email_receipts er SET child_id = NULL WHERE er.child_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM parent_children pc JOIN parent_profiles pp ON pp.id = pc.parent_id JOIN users u ON u.id = pp.user_id WHERE pc.child_id = er.child_id AND lower(er.payer_name) LIKE lower(trim(concat_ws(' ', u.last_name, u.first_name)) || '%'))",
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
                ):
                    await conn.execute(text(sql))

                for name, slug in (
                    ("Математика", "matematika"),
                    ("Физика", "fizika"),
                    ("Английский язык", "angliyskiy"),
                    ("Биология", "biologiya"),
                    ("Химия", "himiya"),
                ):
                    await conn.execute(
                        text(
                            "INSERT INTO subjects (name, slug, is_active) "
                            "VALUES (:name, :slug, TRUE) "
                            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, is_active = TRUE"
                        ),
                        {"name": name, "slug": slug},
                    )

        logger.info("Database schema initialization complete")
    except Exception:
        logger.exception("Database schema initialization failed")
        raise

    task = asyncio.create_task(_daily_email_task())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Пифагор API", version="1.0.0", lifespan=lifespan)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "pifagor-api"}


current_dir = os.path.dirname(os.path.abspath(__file__))

uploads_dir = os.path.join(current_dir, "uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

static_dir = os.path.join(current_dir, "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
