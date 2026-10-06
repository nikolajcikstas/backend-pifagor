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
        # Восстановление пароля по телефону+email — без этого лимита можно
        # было бы перебирать номера телефонов, поэтому ограничиваем жёстче.
        ("POST", "/api/v1/auth/forgot-password/request"): (5, 300),
        ("POST", "/api/v1/auth/forgot-password"): (10, 300),
        ("POST", "/api/v1/change-password"): (10, 300),
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

    # Первую проверку почты делаем не сразу после запуска: сервер на Render
    # «просыпается» именно когда человек входит в кабинет — сначала вход.
    await asyncio.sleep(180)
    while True:
        try:
            async with async_session_maker() as db:
                await run_email_parse(db)
        except Exception as e:
            logger.error("Daily email parse failed: %s", e)
        await asyncio.sleep(86400)


async def _weekly_contract_recalc_task():
    """Каждое воскресенье пересчитывает рекомендации по всем привязанным
    к ученику договорам (сравнивает график платежей с фактически проведёнными
    занятиями)."""
    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
    from app.db.session import async_session_maker
    from app.api.v1.endpoints.admin import recalculate_all_contracts

    while True:
        now = _datetime.utcnow()
        days_until_sunday = (6 - now.weekday()) % 7
        next_run = _datetime.combine(now.date(), _datetime.min.time()) + _timedelta(days=days_until_sunday, hours=6)
        if next_run <= now:
            next_run += _timedelta(days=7)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            async with async_session_maker() as db:
                updated = await recalculate_all_contracts(db, _date.today())
                logger.info("Weekly contract recalculation done: %s contracts updated", updated)
        except Exception as e:
            logger.error("Weekly contract recalculation failed: %s", e)


def _schema_fingerprint() -> str:
    """Отпечаток схемы: код настройки базы + все таблицы и колонки моделей.
    Пока он не меняется, при каждом «пробуждении» сервера проверять и
    дополнять базу не нужно — сервер стартует за секунды, а не за минуту."""
    import hashlib
    import inspect as _inspect
    h = hashlib.sha1()
    try:
        h.update(_inspect.getsource(_init_database_schema).encode("utf-8"))
    except Exception:
        h.update(b"no-source")
    for t in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        h.update(t.name.encode("utf-8"))
        for c in t.columns:
            h.update(f"{c.name}:{c.type}".encode("utf-8"))
    return "schema:" + h.hexdigest()[:20]


async def _schema_is_current(marker: str) -> bool:
    try:
        async with asyncio.timeout(20):
            async with engine.connect() as conn:
                res = await conn.execute(text("SELECT 1 FROM app_data_migrations WHERE name = :n"), {"n": marker})
                return res.first() is not None
    except Exception:
        return False  # таблицы ещё нет или база недоступна — делаем полную настройку


async def _db_sessions_report(kill_stuck: bool) -> None:
    """Кто ещё подключён к базе. Зависшие подключения («idle in transaction»
    дольше 30 секунд — например, от прошлой копии сервера) закрываем: они
    держат таблицы, и добавить новые колонки не получается."""
    try:
        async with asyncio.timeout(15):
            async with engine.connect() as conn:
                rows = (await conn.execute(text(
                    "SELECT pid, state, EXTRACT(EPOCH FROM now() - COALESCE(xact_start, query_start))::int AS age, "
                    "LEFT(REGEXP_REPLACE(query, '\\s+', ' ', 'g'), 120) AS q "
                    "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND backend_type = 'client backend'"
                ))).all()
                for pid, state, age, q in rows:
                    stuck = kill_stuck and state in ("idle in transaction", "idle in transaction (aborted)") and (age or 0) > 30
                    logger.warning("DB session pid=%s state=%s age=%ss%s query=%s", pid, state, age,
                                   " -> closing (stuck)" if stuck else "", q)
                    if stuck:
                        await conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                await conn.commit()
    except Exception as e:
        logger.warning("Could not inspect DB sessions: %s", e)


async def _prepare_database() -> bool:
    marker = _schema_fingerprint()
    if await _schema_is_current(marker):
        logger.info("Database schema is up to date (%s) — skipping initialization", marker)
        return True
    await _db_sessions_report(kill_stuck=True)
    if await _init_database_schema():
        try:
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO app_data_migrations (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"), {"n": marker})
        except Exception:
            logger.exception("Could not store schema marker")
        return True
    return False


async def _schema_retry_task() -> None:
    """Если при запуске настроить базу не удалось (например, при деплое старая
    копия сервера ещё держала таблицы), повторяем в фоне, пока не получится."""
    delay = 20
    for _ in range(30):
        await asyncio.sleep(delay)
        try:
            if await _prepare_database():
                logger.warning("SCHEMA ready on retry")
                return
        except Exception:
            logger.exception("Schema retry failed")
        delay = min(delay * 2, 300)


async def _init_database_schema() -> bool:
    """Best-effort schema bootstrap. Never crash the process on transient DB issues.
    Каждый оператор выполняется в своей короткой транзакции: если один зависнет
    или упадёт, всё уже сделанное сохраняется, а не откатывается целиком.
    Возвращает True, если всё применилось без ошибок."""
    import time as _time
    logger.warning("SCHEMA initialization started")
    failures = 0
    lock_fails = 0

    async def run(label: str, sql: str, params: "dict | None" = None) -> bool:
        nonlocal failures, lock_fails
        t0 = _time.monotonic()
        try:
            async with asyncio.timeout(40):
                async with engine.begin() as conn:
                    # не ждать блокировку бесконечно: при деплое старая копия
                    # сервера может держать таблицу — тогда повторим позже
                    await conn.execute(text("SET LOCAL lock_timeout = '8s'"))
                    await conn.execute(text("SET LOCAL statement_timeout = '30s'"))
                    await conn.execute(text(sql), params or {})
            dt = _time.monotonic() - t0
            if dt > 2:
                logger.warning("SCHEMA slow statement (%.1fs): %s", dt, label[:120])
            return True
        except Exception as e:
            failures += 1
            if "lock timeout" in str(e).lower():
                lock_fails += 1
            logger.warning("SCHEMA statement failed after %.1fs: %s | %s: %s",
                           _time.monotonic() - t0, label[:120], type(e).__name__, str(e)[:200])
            return False

    try:
        t0 = _time.monotonic()
        async with asyncio.timeout(60):
            async with engine.begin() as conn:
                await conn.execute(text("SET LOCAL lock_timeout = '8s'"))
                await conn.run_sync(Base.metadata.create_all)
        logger.warning("SCHEMA create_all done in %.1fs", _time.monotonic() - t0)
    except Exception as e:
        failures += 1
        logger.warning("SCHEMA create_all failed: %s: %s", type(e).__name__, str(e)[:200])

    for sql in (
                    # реферальные ссылки и история цен учеников
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS ref_code VARCHAR(12)",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS ref_sent_at TIMESTAMP",
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_child_profiles_ref_code ON child_profiles (ref_code)",
                    "ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS ref_code VARCHAR(12)",
                    "ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS referrer_child_id INTEGER REFERENCES child_profiles(id) ON DELETE SET NULL",
                    "ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS ref_flag VARCHAR(20)",
                    "ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS child_name VARCHAR(200)",
                    "ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS grade VARCHAR(20)",
                    "ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS phone_norm VARCHAR(20)",
                    "CREATE INDEX IF NOT EXISTS ix_lead_requests_ref_code ON lead_requests (ref_code)",
                    "CREATE INDEX IF NOT EXISTS ix_lead_requests_referrer_child_id ON lead_requests (referrer_child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lead_requests_phone_norm ON lead_requests (phone_norm)",
                    # учёт посещений личного кабинета: время на вкладках
                    "ALTER TABLE lk_events ADD COLUMN IF NOT EXISTS page VARCHAR(20)",
                    "ALTER TABLE lk_events ADD COLUMN IF NOT EXISTS seconds INTEGER",

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
                    # Проверка отчётов админом: старые отчёты остаются видимыми родителям
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'approved'",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP",
                    "CREATE INDEX IF NOT EXISTS ix_reports_tutor_status ON reports (tutor_id, status)",
                    "CREATE INDEX IF NOT EXISTS ix_reports_lesson_id ON reports (lesson_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_tutor_child ON lessons (tutor_id, child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON notifications (user_id, is_read)",
                    # ДЗ: несколько файлов, ответ ученика, оценка репетитора
                    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS task_files TEXT",
                    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS submission_files TEXT",
                    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP",
                    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS grade INTEGER",
                    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS tutor_comment TEXT",
                    "ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS checked_at TIMESTAMP",
                    "CREATE INDEX IF NOT EXISTS ix_homeworks_lesson_id ON homeworks (lesson_id)",
                    "CREATE INDEX IF NOT EXISTS ix_homeworks_child_id ON homeworks (child_id)",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS hw_avg_grade DOUBLE PRECISION",
                    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS hw_count INTEGER",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS qm_trial_call_done BOOLEAN NOT NULL DEFAULT FALSE",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS qm_refusal_reason TEXT",
                    # не больше одной записи «New» на ученика
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_qm_calls_open ON qm_calls (child_id) WHERE status = 'new'",
                    "CREATE INDEX IF NOT EXISTS ix_qm_calls_status_closed ON qm_calls (status, closed_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_date_status ON lessons (date, status)",
                    "UPDATE child_profiles SET crm_status = 'Пробное' WHERE crm_status LIKE 'Р%' OR crm_status IS NULL",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_tutor_id ON lessons (tutor_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_child_id ON lessons (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_subject_id ON lessons (subject_id)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_date ON lessons (date)",
                    "CREATE INDEX IF NOT EXISTS ix_lessons_status ON lessons (status)",
                    "CREATE INDEX IF NOT EXISTS ix_users_first_name ON users (first_name)",
                    "CREATE INDEX IF NOT EXISTS ix_users_last_name ON users (last_name)",
                    "ALTER TABLE parent_contracts ALTER COLUMN parent_id DROP NOT NULL",
                    "ALTER TABLE parent_contracts ALTER COLUMN child_id DROP NOT NULL",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS contract_number VARCHAR(50)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS client_name_raw VARCHAR(300)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS start_date DATE",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS end_date DATE",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS total_amount DOUBLE PRECISION",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS parent_full_name VARCHAR(300)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS parent_phone VARCHAR(50)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS parent_email VARCHAR(200)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS city VARCHAR(200)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS street VARCHAR(200)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS house VARCHAR(50)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS payments_json TEXT",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS match_status VARCHAR(20) NOT NULL DEFAULT 'unmatched'",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS recommendation TEXT",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS recommendation_as_of DATE",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS payment_mode VARCHAR(20) NOT NULL DEFAULT 'unknown'",
                    "ALTER TABLE child_profiles ADD COLUMN IF NOT EXISTS accounting_start_date DATE",
                    # Индексы для ускорения дашбордов (СРМ, договоры, оплаты) —
                    # без них запросы линейно замедляются с ростом числа учеников.
                    "CREATE INDEX IF NOT EXISTS ix_parent_contracts_child_id ON parent_contracts (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_parent_contracts_match_status ON parent_contracts (match_status)",
                    "CREATE INDEX IF NOT EXISTS ix_parent_contract_children_contract_id ON parent_contract_children (contract_id)",
                    "CREATE INDEX IF NOT EXISTS ix_parent_contract_children_child_id ON parent_contract_children (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_email_receipts_child_id ON email_receipts (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_email_receipt_splits_receipt_id ON email_receipt_splits (receipt_id)",
                    "CREATE INDEX IF NOT EXISTS ix_email_receipt_splits_child_id ON email_receipt_splits (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_child_profiles_crm_status ON child_profiles (crm_status)",
                    "CREATE INDEX IF NOT EXISTS ix_parent_children_parent_id ON parent_children (parent_id)",
                    "CREATE INDEX IF NOT EXISTS ix_parent_children_child_id ON parent_children (child_id)",
                    "CREATE INDEX IF NOT EXISTS ix_tutor_rate_history_tutor_id ON tutor_rate_history (tutor_id)",
                    "CREATE INDEX IF NOT EXISTS ix_tutor_payouts_tutor_id ON tutor_payouts (tutor_id)",
                    "CREATE INDEX IF NOT EXISTS ix_users_role ON users (role)",
                    # Файл договора хранится прямо в БД — переживает передеплой
                    # на Render (локальный диск эфемерный и очищается при каждом деплое).
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS file_data BYTEA",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS file_mime VARCHAR(150)",
                    "ALTER TABLE parent_contracts ADD COLUMN IF NOT EXISTS file_name VARCHAR(255)",
    ):
        await run(sql, sql)
        if lock_fails >= 2:
            logger.warning("SCHEMA: tables are busy (another server copy is still running) — will retry shortly")
            break

    for name, slug in (
        ("Математика", "matematika"),
        ("Физика", "fizika"),
        ("Английский язык", "angliyskiy"),
        ("Русский язык", "russkiy"),
        ("Белорусский язык", "belorusskiy"),
        ("Биология", "biologiya"),
        ("Химия", "himiya"),
    ):
        await run(f"subject {slug}",
                  "INSERT INTO subjects (name, slug, is_active) VALUES (:name, :slug, TRUE) "
                  "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, is_active = TRUE",
                  {"name": name, "slug": slug})

    # Разовые правки данных: каждая выполняется ровно один раз
    # (отметка о выполнении хранится в таблице app_data_migrations).
    try:
        async with asyncio.timeout(40):
            async with engine.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS app_data_migrations ("
                    "name VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMP DEFAULT now())"
                ))
                first_time = (await conn.execute(text(
                    "INSERT INTO app_data_migrations (name) VALUES ('reports_back_to_review_2026_09') "
                    "ON CONFLICT (name) DO NOTHING RETURNING name"
                ))).first()
                if first_time:
                    # Все уже отправленные отчёты возвращаются на проверку администратору:
                    # у родителей они скрываются до повторной отправки.
                    res = await conn.execute(text(
                        "UPDATE reports SET status = 'submitted', approved_at = NULL "
                        "WHERE status = 'approved' AND COALESCE(TRIM(content), '') <> ''"
                    ))
                    logger.info("Reports returned to review: %s", res.rowcount)
    except Exception as e:
        failures += 1
        logger.warning("SCHEMA one-time data migration failed: %s: %s", type(e).__name__, str(e)[:200])

    logger.warning("SCHEMA initialization finished (failures: %s)", failures)
    return failures == 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    schema_ok = await _prepare_database()
    from app.api.v1.endpoints.quality import qm_scheduler_task
    task = asyncio.create_task(_daily_email_task())
    qm_task = asyncio.create_task(qm_scheduler_task())
    tasks = [task, qm_task]
    if not schema_ok:
        tasks.append(asyncio.create_task(_schema_retry_task()))
    yield
    for t in tasks:
        t.cancel()
        try:
            await t
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


current_dir = os.path.dirname(os.path.abspath(__file__))

uploads_dir = os.path.join(current_dir, "uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

static_dir = os.path.join(current_dir, "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
