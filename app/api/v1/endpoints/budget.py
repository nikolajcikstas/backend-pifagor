"""Бюджет компании: зарплаты с налогами, налоги, ЧП, безубыточность.

Защита данных:
• Все настройки бюджета (зарплаты сотрудников, оформление и вычеты
  репетиторов, константы, суммы месяцев) хранятся в базе только
  в зашифрованном виде — AES-256-GCM.
• Ключ шифрования данных случайный; в базе он лежит «завёрнутым» ключом,
  который получается из пароля бюджета (scrypt, 32 МБ памяти на попытку)
  и секрета сервера. Сам пароль нигде не хранится — без него данные не
  прочитать даже при полном доступе к базе.
• Открыть бюджет может только администратор, который знает пароль бюджета.
  После ввода пароля выдаётся временный ключ доступа: 15 минут без действий
  или 4 часа всего — и бюджет снова закрыт.
• 5 неверных паролей подряд — бюджет блокируется на 15 минут.
• Каждый вход, неудачная попытка и сохранение пишутся в журнал.
• Ответы не кэшируются браузером (Cache-Control: no-store).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time as _time
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.core.deps import require_admin
from app.core.security import averify_password
from app.db.session import get_db
from app.models.models import (
    BudgetAudit, BudgetBlob, BudgetVault, Lesson, LessonStatus, TutorProfile, TutorRateHistory, User,
)
from app.services.budget_calc import DEFAULT_CONSTS, compute, consts_of, r2
from app.services.pricing import load_prices, minsk_today

router = APIRouter(prefix="/budget", tags=["budget"])
logger = logging.getLogger(__name__)

KDF_N, KDF_R, KDF_P = 2 ** 15, 8, 1
KDF_NAME = f"scrypt${KDF_N}${KDF_R}${KDF_P}$pep1"
IDLE_SECONDS = 15 * 60
ABSOLUTE_SECONDS = 4 * 3600
MAX_FAILS = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10

# временные ключи доступа: sha256(токен) -> сессия; живут только в памяти сервера
_SESSIONS: dict[str, dict] = {}


# ─── шифрование ───────────────────────────────────────────────────────────────

def _pepper() -> bytes:
    secret = (os.getenv("BUDGET_PEPPER") or app_settings.SECRET_KEY or "").encode("utf-8")
    return hmac.new(secret, b"pifagor-budget-pepper-v1", hashlib.sha256).digest()


def _kek(password: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=KDF_N, r=KDF_R, p=KDF_P,
                         maxmem=128 * 1024 * 1024, dklen=32)
    return hmac.new(_pepper(), raw, hashlib.sha256).digest()


def _wrap(kek: bytes, dk: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(kek).encrypt(nonce, dk, b"pifagor-budget-dk-v1")


def _unwrap(kek: bytes, wrapped: bytes) -> bytes:
    return AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], b"pifagor-budget-dk-v1")


def _seal(dk: bytes, name: str, obj) -> bytes:
    nonce = secrets.token_bytes(12)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"v1" + nonce + AESGCM(dk).encrypt(nonce, data, b"pifagor-budget-blob:" + name.encode())


def _open(dk: bytes, name: str, blob: bytes):
    if blob[:2] != b"v1":
        raise ValueError("unknown blob version")
    return json.loads(AESGCM(dk).decrypt(blob[2:14], blob[14:], b"pifagor-budget-blob:" + name.encode()))


# ─── сессии и журнал ──────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "").split(",")[0].strip()[:64]


def _who(u: User) -> str:
    return (f"{u.last_name or ''} {u.first_name or ''}".strip() or u.email or f"#{u.id}")[:200]


async def _audit(db: AsyncSession, user: Optional[User], action: str, ok: bool, request: Request) -> None:
    db.add(BudgetAudit(user_id=user.id if user else None, who=_who(user) if user else None,
                       action=action, ok=ok, ip=_client_ip(request), at=datetime.utcnow()))


def _purge_sessions() -> None:
    now = _time.monotonic()
    for k in [k for k, s in _SESSIONS.items() if s["idle"] < now or s["abs"] < now]:
        _SESSIONS.pop(k, None)


def _new_session(user: User, dk: bytes) -> str:
    _purge_sessions()
    token = secrets.token_urlsafe(32)
    now = _time.monotonic()
    _SESSIONS[hashlib.sha256(token.encode()).hexdigest()] = {
        "uid": user.id, "dk": dk, "idle": now + IDLE_SECONDS, "abs": now + ABSOLUTE_SECONDS}
    return token


def _drop_sessions() -> None:
    _SESSIONS.clear()


class Unlocked:
    def __init__(self, user: User, dk: bytes):
        self.user, self.dk = user, dk


async def require_unlocked(
    response: Response,
    x_budget_token: Optional[str] = Header(None),
    user: User = Depends(require_admin),
) -> Unlocked:
    """Админ + действующий ключ доступа к бюджету, выданный этому же админу."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    _purge_sessions()
    s = _SESSIONS.get(hashlib.sha256((x_budget_token or "").encode()).hexdigest()) if x_budget_token else None
    if not s or s["uid"] != user.id:
        raise HTTPException(status_code=423, detail="Бюджет закрыт — введите пароль бюджета")
    s["idle"] = _time.monotonic() + IDLE_SECONDS
    return Unlocked(user, s["dk"])


# ─── настройки бюджета (то, что шифруется) ────────────────────────────────────

class Consts(BaseModel):
    usn_pct: float = Field(DEFAULT_CONSTS["usn_pct"], ge=0, le=50)
    easypay_pct: float = Field(DEFAULT_CONSTS["easypay_pct"], ge=0, le=20)
    accountant: float = Field(DEFAULT_CONSTS["accountant"], ge=0, le=1_000_000)
    income_tax_pct: float = Field(DEFAULT_CONSTS["income_tax_pct"], ge=0, le=60)
    fszn_pct: float = Field(DEFAULT_CONSTS["fszn_pct"], ge=0, le=80)
    pension_pct: float = Field(DEFAULT_CONSTS["pension_pct"], ge=0, le=20)
    deduction: float = Field(DEFAULT_CONSTS["deduction"], ge=0, le=100_000)
    deduction_limit: float = Field(DEFAULT_CONSTS["deduction_limit"], ge=0, le=1_000_000)
    belgosstrakh_pct: float = Field(DEFAULT_CONSTS["belgosstrakh_pct"], ge=0, le=10)


class TutorFlags(BaseModel):
    formal: bool = True
    deduction: bool = False


BASE_MONTH = "2000-01"  # запись «с самого начала»
MONTH_RE = r"20\d\d-(0[1-9]|1[0-2])"


class FlagEntry(BaseModel):
    """Оформление и вычет с 1-го числа месяца `month` (и дальше, пока нет более поздней записи)."""
    month: str = Field(..., pattern=MONTH_RE)
    formal: bool = True
    deduction: bool = False


def _as_hist(v) -> list:
    """Старый формат {formal, deduction} → история из одной записи «с самого начала»."""
    if isinstance(v, dict) and "month" not in v:
        return [{"month": BASE_MONTH, "formal": bool(v.get("formal", True)), "deduction": bool(v.get("deduction", False))}]
    if isinstance(v, list):
        return v
    return []


def flags_at(hist: list, month: str) -> Optional[TutorFlags]:
    """Флаги, действующие в месяце `month`; None — если для этого месяца записей нет."""
    best = None
    for e in hist or []:
        if e.month <= month and (best is None or e.month >= best.month):
            best = e
    return TutorFlags(formal=best.formal, deduction=best.deduction) if best else None


def set_flag(hist: list, month: str, formal: Optional[bool], deduction: Optional[bool]) -> list:
    """Новое значение действует с 1-го числа `month` и на все следующие месяцы:
    более поздние записи убираются, прошлые месяцы не меняются."""
    cur = flags_at(hist, month) or TutorFlags()
    new = FlagEntry(month=month, formal=cur.formal if formal is None else formal,
                    deduction=cur.deduction if deduction is None else deduction)
    if not new.formal:
        new.deduction = False
    return sorted([e for e in hist if e.month < month] + [new], key=lambda e: e.month)


class StaffMember(BaseModel):
    id: str = Field(..., min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=100)
    formal: bool = True          # для новых сотрудников — значение «с самого начала»
    deduction: bool = False
    net_default: float = Field(0, ge=0, le=1_000_000)
    hist: list[FlagEntry] = Field(default_factory=list, max_length=240)

    def flags(self, month: str) -> TutorFlags:
        return flags_at(self.hist, month) or TutorFlags(formal=self.formal, deduction=self.deduction)


class MonthVals(BaseModel):
    advertising: float = Field(0, ge=0, le=10_000_000)
    fund: float = Field(0, ge=0, le=10_000_000)
    next_month: float = Field(0, ge=0, le=10_000_000)
    staff: dict[str, float] = Field(default_factory=dict)

    @field_validator("staff")
    @classmethod
    def _staff_ok(cls, v):
        if len(v) > 50:
            raise ValueError("слишком много сотрудников")
        for k, x in v.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", k) or x is None or x < 0 or x > 1_000_000:
                raise ValueError("неверная сумма сотрудника")
        return v


class BudgetSettings(BaseModel):
    consts: Consts = Field(default_factory=Consts)
    tutors: dict[str, list[FlagEntry]] = Field(default_factory=dict)
    staff: list[StaffMember] = Field(default_factory=list, max_length=50)
    months: dict[str, MonthVals] = Field(default_factory=dict)

    @field_validator("months")
    @classmethod
    def _months_ok(cls, v):
        if len(v) > 240:
            raise ValueError("слишком много месяцев")
        for k in v:
            if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", k):
                raise ValueError("месяц в формате ГГГГ-ММ")
        return v

    @field_validator("tutors", mode="before")
    @classmethod
    def _tutors_legacy(cls, v):
        return {k: _as_hist(x) for k, x in (v or {}).items()}

    @field_validator("tutors")
    @classmethod
    def _tutors_ok(cls, v):
        for k in v:
            if not k.isdigit():
                raise ValueError("неверный репетитор")
        return v


async def _load_settings(db: AsyncSession, dk: bytes) -> BudgetSettings:
    blob = await db.get(BudgetBlob, "settings")
    if not blob:
        return BudgetSettings()
    return BudgetSettings.model_validate(_open(dk, "settings", blob.data))


def _month_vals(st: BudgetSettings, month: str) -> MonthVals:
    """Значения месяца; если месяц ещё не заполняли — как в последнем заполненном
    до него (реклама, фонд, зарплаты сотрудников обычно не меняются каждый месяц)."""
    if month in st.months:
        return st.months[month]
    prev = sorted(k for k in st.months if k < month)
    if prev:
        return st.months[prev[-1]]
    return MonthVals(staff={s.id: s.net_default for s in st.staff})


# ─── данные месяца из базы ────────────────────────────────────────────────────

def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return start, end


async def _month_facts(db: AsyncSession, month: str) -> dict:
    """Выручка (проведённые занятия × цена ученика на дату), часы и заработок
    репетиторов (занятия × ставка на дату — те же правила, что во вкладке
    «Репетиторы»)."""
    start, end = _month_bounds(month)
    today = minsk_today()
    last = min(end, today)
    rows = (await db.execute(
        select(Lesson.child_id, Lesson.tutor_id, Lesson.date, Lesson.time_start, Lesson.time_end)
        .where(Lesson.status == LessonStatus.completed, Lesson.date >= start, Lesson.date <= last)
    )).all() if last >= start else []
    prices = await load_prices(db, [r[0] for r in rows])
    hist: dict[int, tuple[list, list]] = {}
    for tid, eff, rate in (await db.execute(
        select(TutorRateHistory.tutor_id, TutorRateHistory.effective_from, TutorRateHistory.rate_per_hour)
        .order_by(TutorRateHistory.tutor_id, TutorRateHistory.effective_from, TutorRateHistory.id)
    )).all():
        ds, rs = hist.setdefault(tid, ([], []))
        if ds and ds[-1] == eff:
            rs[-1] = rate
        else:
            ds.append(eff)
            rs.append(rate)
    tutors = {tid: (ln, fn, rate) for tid, ln, fn, rate in (await db.execute(
        select(TutorProfile.id, User.last_name, User.first_name, TutorProfile.rate_per_hour)
        .join(User, User.id == TutorProfile.user_id)
    )).all()}

    def rate_at(tid: int, d: date) -> float:
        h = hist.get(tid)
        if h:
            i = bisect_right(h[0], d)
            return float(h[1][i - 1] if i else h[1][0])
        return float((tutors.get(tid) or (None, None, 0))[2] or 0)

    per = defaultdict(lambda: {"net": 0.0, "hours": 0.0, "lessons": 0})
    revenue = hours = 0.0
    for cid, tid, d, ts, te in rows:
        dur = max(0.0, (datetime.combine(d, te) - datetime.combine(d, ts)).total_seconds() / 3600)
        revenue += prices.price(cid, d)
        hours += dur
        p = per[tid]
        p["net"] += rate_at(tid, d)
        p["hours"] += dur
        p["lessons"] += 1
    tl = []
    for tid, p in per.items():
        ln, fn, _r = tutors.get(tid, ("", "", 0))
        tl.append({"id": tid, "name": f"{ln or ''} {fn or ''}".strip() or f"#{tid}",
                   "net": r2(p["net"]), "hours": round(p["hours"], 2), "lessons": p["lessons"]})
    tl.sort(key=lambda x: -x["net"])
    return {"revenue": r2(revenue), "hours": round(hours, 2), "lessons": len(rows), "tutors": tl,
            "partial": end >= today, "start": start.isoformat(), "end": end.isoformat()}


def _inputs(facts: dict, st: BudgetSettings, month: str) -> dict:
    mv = _month_vals(st, month)
    tutors = []
    for t in facts["tutors"]:
        fl = flags_at(st.tutors.get(str(t["id"])) or [], month) or TutorFlags()
        tutors.append({**t, "formal": fl.formal, "deduction": fl.deduction})
    staff = [{"id": s.id, "name": s.name, "formal": s.flags(month).formal, "deduction": s.flags(month).deduction,
              "net": mv.staff.get(s.id, s.net_default)} for s in st.staff]
    return {"revenue": facts["revenue"], "hours": facts["hours"], "lessons": facts["lessons"],
            "tutors": tutors, "staff": staff, "consts": st.consts.model_dump(),
            "month": {"advertising": mv.advertising, "fund": mv.fund, "next_month": mv.next_month}}


# ─── доступ ───────────────────────────────────────────────────────────────────

class SetupIn(BaseModel):
    password: str = Field(..., min_length=MIN_PASSWORD, max_length=200)
    account_password: str = Field(..., min_length=1, max_length=500)


class UnlockIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


class ChangeIn(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=MIN_PASSWORD, max_length=200)


class ResetIn(BaseModel):
    account_password: str = Field(..., min_length=1, max_length=500)
    confirm: str


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


@router.get("/status")
async def budget_status(response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    _no_store(response)
    v = (await db.execute(select(BudgetVault).order_by(BudgetVault.id).limit(1))).scalars().first()
    locked = v.locked_until if v and v.locked_until and v.locked_until > datetime.utcnow() else None
    return {"configured": v is not None,
            "created_by": v.created_by if v else None,
            "created_at": v.created_at.isoformat() + "Z" if v and v.created_at else None,
            "locked_until": locked.isoformat() + "Z" if locked else None,
            "min_password": MIN_PASSWORD}


# Кто оформлен и у кого вычет — по ведомости бухгалтера за сентябрь 2026:
# все репетиторы из неё оформлены, вычет 216 р у всех, кроме Федоренко Андрея.
_SEPT_SHEET = [
    ("шпакова", "ксения"), ("тихонович", "ульяна"), ("федоренко", "ксения"), ("рапчинский", "владислав"),
    ("матюхин", None), ("борщов", "сергей"), ("федоренко", "андрей"), ("мухарский", None), ("бобр", None),
    ("пунтус", None), ("меньшикова", None), ("евтеев", None), ("булыгина", None), ("ермолович", None),
    ("храпко", None), ("шаповал", None),
]


async def _seed_tutor_flags(db: AsyncSession) -> dict:
    rows = (await db.execute(
        select(TutorProfile.id, User.last_name, User.first_name).join(User, User.id == TutorProfile.user_id)
    )).all()
    out = {}
    for ln, fn in _SEPT_SHEET:
        hits = [r for r in rows if (r[1] or "").strip().lower() == ln
                and (fn is None or (r[2] or "").strip().lower() == fn)]
        if len(hits) == 1:
            tid = hits[0][0]
            out[str(tid)] = [FlagEntry(month=BASE_MONTH, formal=True, deduction=not (ln == "федоренко" and fn == "андрей"))]
    return out


@router.post("/setup")
async def budget_setup(body: SetupIn, request: Request, response: Response,
                       db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    """Первый запуск: придумать пароль бюджета. Нужен и пароль от своего аккаунта."""
    _no_store(response)
    if await db.scalar(select(func.count(BudgetVault.id))):
        raise HTTPException(status_code=409, detail="Пароль бюджета уже задан")
    if not await averify_password(body.account_password, user.hashed_password):
        await _audit(db, user, "setup", False, request)
        await db.commit()
        raise HTTPException(status_code=403, detail="Неверный пароль от аккаунта")
    if body.password == body.account_password:
        raise HTTPException(status_code=400, detail="Пароль бюджета должен отличаться от пароля входа")
    salt, dk = secrets.token_bytes(16), secrets.token_bytes(32)
    kek = await asyncio.to_thread(_kek, body.password, salt)
    db.add(BudgetVault(salt=salt, kdf=KDF_NAME, wrapped_key=_wrap(kek, dk), created_by=_who(user),
                       created_at=datetime.utcnow(), failed=0))
    seeded = await _seed_tutor_flags(db)
    await db.execute(delete(BudgetBlob))
    db.add(BudgetBlob(name="settings", data=_seal(dk, "settings", BudgetSettings(tutors=seeded).model_dump()),
                      updated_at=datetime.utcnow(), updated_by=_who(user)))
    await _audit(db, user, "setup", True, request)
    await db.commit()
    return {"token": _new_session(user, dk), "idle_seconds": IDLE_SECONDS}


@router.post("/unlock")
async def budget_unlock(body: UnlockIn, request: Request, response: Response,
                        db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    _no_store(response)
    v = (await db.execute(select(BudgetVault).order_by(BudgetVault.id).limit(1).with_for_update())).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="Пароль бюджета ещё не задан")
    now = datetime.utcnow()
    if v.locked_until and v.locked_until > now:
        mins = max(1, int((v.locked_until - now).total_seconds() // 60) + 1)
        raise HTTPException(status_code=429, detail=f"Слишком много неверных паролей. Попробуйте через {mins} мин.")
    kek = await asyncio.to_thread(_kek, body.password, v.salt)
    try:
        dk = _unwrap(kek, v.wrapped_key)
    except Exception:
        v.failed = (v.failed or 0) + 1
        left = MAX_FAILS - v.failed
        if v.failed >= MAX_FAILS:
            v.locked_until, v.failed = now + timedelta(minutes=LOCK_MINUTES), 0
        await _audit(db, user, "unlock", False, request)
        await db.commit()
        if left <= 0:
            raise HTTPException(status_code=429, detail=f"Слишком много неверных паролей. Бюджет закрыт на {LOCK_MINUTES} минут.")
        raise HTTPException(status_code=403, detail=f"Неверный пароль бюджета. Осталось попыток: {left}")
    v.failed, v.locked_until = 0, None
    await _audit(db, user, "unlock", True, request)
    await db.commit()
    return {"token": _new_session(user, dk), "idle_seconds": IDLE_SECONDS}


@router.post("/lock")
async def budget_lock(response: Response, x_budget_token: Optional[str] = Header(None), user: User = Depends(require_admin)):
    _no_store(response)
    if x_budget_token:
        _SESSIONS.pop(hashlib.sha256(x_budget_token.encode()).hexdigest(), None)
    return {"ok": True}


@router.post("/password")
async def budget_change_password(body: ChangeIn, request: Request, db: AsyncSession = Depends(get_db),
                                 u: Unlocked = Depends(require_unlocked)):
    v = (await db.execute(select(BudgetVault).order_by(BudgetVault.id).limit(1))).scalars().first()
    kek_old = await asyncio.to_thread(_kek, body.old_password, v.salt)
    try:
        dk = _unwrap(kek_old, v.wrapped_key)
    except Exception:
        await _audit(db, u.user, "password", False, request)
        await db.commit()
        raise HTTPException(status_code=403, detail="Старый пароль бюджета неверный")
    salt = secrets.token_bytes(16)
    kek = await asyncio.to_thread(_kek, body.new_password, salt)
    v.salt, v.kdf, v.wrapped_key = salt, KDF_NAME, _wrap(kek, dk)
    await _audit(db, u.user, "password", True, request)
    await db.commit()
    _drop_sessions()
    return {"token": _new_session(u.user, dk), "idle_seconds": IDLE_SECONDS}


@router.post("/reset")
async def budget_reset(body: ResetIn, request: Request, response: Response,
                       db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    """Забыли пароль бюджета: настройки бюджета удаляются (прочитать их без
    пароля невозможно), можно задать новый пароль и заполнить заново."""
    _no_store(response)
    if body.confirm.strip().upper() != "СБРОСИТЬ":
        raise HTTPException(status_code=400, detail="Для сброса введите слово СБРОСИТЬ")
    if not await averify_password(body.account_password, user.hashed_password):
        await _audit(db, user, "reset", False, request)
        await db.commit()
        raise HTTPException(status_code=403, detail="Неверный пароль от аккаунта")
    await db.execute(delete(BudgetBlob))
    await db.execute(delete(BudgetVault))
    await _audit(db, user, "reset", True, request)
    await db.commit()
    _drop_sessions()
    return {"ok": True}


# ─── данные ───────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_budget_settings(month: Optional[str] = Query(None), db: AsyncSession = Depends(get_db),
                              u: Unlocked = Depends(require_unlocked)):
    """Настройки; галочки репетиторов — действующие в месяце `month` (по умолчанию — текущий)."""
    st = await _load_settings(db, u.dk)
    t0 = minsk_today()
    month = month if month and re.fullmatch(MONTH_RE, month) else f"{t0.year:04d}-{t0.month:02d}"
    tutors = [{"id": tid, "name": f"{ln or ''} {fn or ''}".strip(), "active": act is not False,
               **(flags_at(st.tutors.get(str(tid)) or [], month) or TutorFlags()).model_dump(),
               "set": bool(st.tutors.get(str(tid)))}
              for tid, ln, fn, act in (await db.execute(
                  select(TutorProfile.id, User.last_name, User.first_name, User.is_active)
                  .join(User, User.id == TutorProfile.user_id).order_by(User.last_name, User.first_name)
              )).all()]
    return {"settings": st.model_dump(), "tutors": tutors, "defaults": DEFAULT_CONSTS, "month": month}


async def _save_settings(db: AsyncSession, u: "Unlocked", st: BudgetSettings) -> None:
    data = _seal(u.dk, "settings", st.model_dump())
    blob = await db.get(BudgetBlob, "settings")
    if blob:
        blob.data, blob.updated_at, blob.updated_by = data, datetime.utcnow(), _who(u.user)
    else:
        db.add(BudgetBlob(name="settings", data=data, updated_at=datetime.utcnow(), updated_by=_who(u.user)))


@router.put("/settings")
async def put_budget_settings(body: BudgetSettings, request: Request, db: AsyncSession = Depends(get_db),
                              u: Unlocked = Depends(require_unlocked)):
    """Константы, сотрудники (имена, суммы) и значения месяцев. Галочки
    «оформлен / вычет» меняются только через /flag — здесь берутся сохранённые."""
    cur = await _load_settings(db, u.dk)
    old_staff = {s.id: s for s in cur.staff}
    staff = []
    for s in body.staff:
        o = old_staff.get(s.id)
        staff.append(s.model_copy(update={"formal": o.formal, "deduction": o.deduction, "hist": o.hist}) if o else
                     s.model_copy(update={"hist": []}))
    st = body.model_copy(update={"tutors": cur.tutors, "staff": staff})
    await _save_settings(db, u, st)
    await _audit(db, u.user, "save", True, request)
    await db.commit()
    return {"ok": True}


class FlagIn(BaseModel):
    kind: str = Field(..., pattern=r"^(tutor|staff)$")
    id: str = Field(..., min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    month: str = Field(..., pattern=MONTH_RE)
    formal: Optional[bool] = None
    deduction: Optional[bool] = None


@router.put("/flag")
async def put_budget_flag(body: FlagIn, request: Request, db: AsyncSession = Depends(get_db),
                          u: Unlocked = Depends(require_unlocked)):
    """«Оформлен» / «вычет» с 1-го числа месяца: этот месяц и следующие
    пересчитываются так, будто человек был в таком статусе весь месяц."""
    st = await _load_settings(db, u.dk)
    if body.kind == "tutor":
        if not body.id.isdigit() or not await db.get(TutorProfile, int(body.id)):
            raise HTTPException(status_code=404, detail="Репетитор не найден")
        st.tutors[body.id] = set_flag(st.tutors.get(body.id) or [], body.month, body.formal, body.deduction)
    else:
        idx = next((i for i, s in enumerate(st.staff) if s.id == body.id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Сотрудник не найден")
        s = st.staff[idx]
        hist = s.hist or [FlagEntry(month=BASE_MONTH, formal=s.formal, deduction=s.deduction)]
        st.staff[idx] = s.model_copy(update={"hist": set_flag(hist, body.month, body.formal, body.deduction)})
    await _save_settings(db, u, st)
    await _audit(db, u.user, "save", True, request)
    await db.commit()
    return {"ok": True}


def _month_ok(month: Optional[str]) -> str:
    if month and re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
        return month
    t = minsk_today()
    prev = date(t.year, t.month, 1) - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


@router.get("/report")
async def budget_report(month: Optional[str] = Query(None), db: AsyncSession = Depends(get_db),
                        u: Unlocked = Depends(require_unlocked)):
    month = _month_ok(month)
    st = await _load_settings(db, u.dk)
    facts = await _month_facts(db, month)
    res = compute(_inputs(facts, st, month))
    # динамика за 6 месяцев (по сохранённым настройкам каждого месяца)
    y, m = int(month[:4]), int(month[5:7])
    trend = []
    for k in range(5, -1, -1):
        mm = (y * 12 + m - 1) - k
        key = f"{mm // 12:04d}-{mm % 12 + 1:02d}"
        f = facts if key == month else await _month_facts(db, key)
        r = res if key == month else compute(_inputs(f, st, key), with_extras=False)
        trend.append({"month": key, "revenue": r["revenue"], "profit": r["profit"], "chp": r["chp"],
                      "hours": r["hours"], "chp_per_hour": r["chp_per_hour"], "partial": f["partial"],
                      "filled": key in st.months})
    flags_missing = [t["name"] for t in facts["tutors"] if not st.tutors.get(str(t["id"]))]
    return {"month": month, "start": facts["start"], "end": facts["end"], "partial": facts["partial"],
            "filled": month in st.months, "flags_missing": flags_missing,
            "month_values": _month_vals(st, month).model_dump(), **res, "trend": trend}


@router.get("/audit")
async def budget_audit(db: AsyncSession = Depends(get_db), u: Unlocked = Depends(require_unlocked)):
    rows = (await db.execute(select(BudgetAudit).order_by(BudgetAudit.at.desc()).limit(60))).scalars().all()
    names = {"setup": "Пароль бюджета задан", "unlock": "Вход в бюджет", "save": "Сохранение",
             "password": "Смена пароля бюджета", "reset": "Сброс бюджета"}
    return [{"at": r.at.isoformat() + "Z", "who": r.who, "action": names.get(r.action, r.action), "ok": r.ok, "ip": r.ip}
            for r in rows]
