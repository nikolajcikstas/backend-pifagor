import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from app.api.v1.router import api_router
from app.api.v1.endpoints import admin
from app.db.session import engine, Base

logger = logging.getLogger(__name__)

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
                await conn.execute(text(
                    "ALTER TABLE child_profiles "
                    "ADD COLUMN IF NOT EXISTS lesson_price DOUBLE PRECISION NOT NULL DEFAULT 40"
                ))
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
