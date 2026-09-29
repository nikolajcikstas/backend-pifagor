"""Контроль качества (Quality system) для менеджера по качеству.

Вкладки:
  • Звонки — кого нужно обзвонить (new) и совершённые звонки (all);
  • Регулярность — клиенты без занятий 7+ дней и контроль «следующего занятия»;
  • Отказы — ученики со статусом «Отказ» в CRM: причина отказа и последний звонок.

Плановые обновления (время минское, UTC+3):
  • вторник и суббота 08:00 — новые звонки и свежая выгрузка «Регулярности»;
  • каждый день после 00:00 — проверка дат «следующего занятия».
Сервер на Render может «спать», поэтому пропущенное обновление выполняется
при первом обращении к разделу (и фоновой задачей, пока сервер не спит).
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from app.core.deps import require_admin
from app.db.session import get_db
from app.models.models import (
    ChildProfile, User, Lesson, LessonStatus, ParentChild, ParentProfile, TutorProfile,
    QmCall, QmRegularity, QmJob,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quality", tags=["quality"])

MINSK = timezone(timedelta(hours=3))
CLIENT = "Клиент"
NOT_CALLED = ("Отказ", "Не занимаются")
REASONS = {"trial_2w": "2 недели после пробного", "quality": "Контроль качества"}
FEEDBACK = ("green", "yellow", "orange", "red")
REG_STATUSES = ("waiting", "in_work", "closed")
TRIAL_DAYS = 14
INACTIVE_DAYS = 7
REFRESH_WEEKDAYS = (1, 5)  # вторник, суббота
REFRESH_HOUR = 8

_lock = asyncio.Lock()
_failed_at: dict[str, datetime] = {}  # задача упала — повторяем не чаще раза в 10 минут


# ─── время и расписание ───────────────────────────────────────────────────────

def _now_minsk() -> datetime:
    return datetime.now(MINSK)


def _to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _last_refresh_slot(now: datetime) -> datetime:
    """Последний момент «вторник/суббота 08:00» не позже now (минское время)."""
    for back in range(0, 8):
        d = (now - timedelta(days=back)).date()
        slot = datetime(d.year, d.month, d.day, REFRESH_HOUR, tzinfo=MINSK)
        if d.weekday() in REFRESH_WEEKDAYS and slot <= now:
            return slot
    return now  # недостижимо


def _next_refresh_slot(now: datetime) -> datetime:
    for fwd in range(0, 8):
        d = (now + timedelta(days=fwd)).date()
        slot = datetime(d.year, d.month, d.day, REFRESH_HOUR, tzinfo=MINSK)
        if d.weekday() in REFRESH_WEEKDAYS and slot > now:
            return slot
    return now


def _last_nightly_slot(now: datetime) -> datetime:
    slot = datetime(now.year, now.month, now.day, 0, 5, tzinfo=MINSK)
    return slot if slot <= now else slot - timedelta(days=1)


def _minus_months(d: datetime, months: int) -> datetime:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    last_day = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return d.replace(year=y, month=m, day=min(d.day, last_day))


def _name(u: Optional[User]) -> str:
    return f"{u.last_name or ''} {u.first_name or ''}".strip() if u else ""


def _real_phone(p: Optional[str]) -> str:
    """+375000000000 подставлялся при регистрации вместо телефона — такой не показываем."""
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    if not digits or set(digits.removeprefix("375")) <= {"0"}:
        return ""
    return p or ""


def _eligible(child: ChildProfile) -> bool:
    return bool(child.user) and child.user.is_active is not False


# ─── плановые задачи ──────────────────────────────────────────────────────────

async def _children(db: AsyncSession) -> list[ChildProfile]:
    res = await db.execute(select(ChildProfile).options(joinedload(ChildProfile.user)))
    return list(res.scalars().unique().all())


async def _anchor_dates(db: AsyncSession, child_ids: Optional[list[int]] = None) -> dict[int, date]:
    """Дата пробного занятия ученика (первое пробное; если пробных не было —
    первое занятие вообще). Отменённые и перенесённые не учитываются."""
    held = [Lesson.status.notin_([LessonStatus.cancelled, LessonStatus.rescheduled])]
    if child_ids is not None:
        held.append(Lesson.child_id.in_(child_ids))
    trial = await db.execute(
        select(Lesson.child_id, func.min(Lesson.date))
        .where(*held, or_(Lesson.is_free_trial.is_(True), Lesson.status == LessonStatus.trial))
        .group_by(Lesson.child_id)
    )
    anchors = {cid: d for cid, d in trial.all()}
    first = await db.execute(select(Lesson.child_id, func.min(Lesson.date)).where(*held).group_by(Lesson.child_id))
    for cid, d in first.all():
        anchors.setdefault(cid, d)
    return anchors


async def _last_completed(db: AsyncSession) -> dict[int, date]:
    res = await db.execute(
        select(Lesson.child_id, func.max(Lesson.date))
        .where(Lesson.status == LessonStatus.completed)
        .group_by(Lesson.child_id)
    )
    return {cid: d for cid, d in res.all()}


async def refresh_calls(db: AsyncSession, initial: bool) -> int:
    """Добавляет в «new» учеников, которым пора позвонить. Возвращает число новых записей."""
    today = _now_minsk().date()
    now_utc = datetime.utcnow()
    children = await _children(db)
    anchors = await _anchor_dates(db)
    open_ids = set((await db.execute(select(QmCall.child_id).where(QmCall.status == "new"))).scalars().all())
    last_done = {
        cid: d for cid, d in (await db.execute(
            select(QmCall.child_id, func.max(QmCall.closed_at))
            .where(QmCall.status.in_(("done", "dismissed"))).group_by(QmCall.child_id)
        )).all()
    }
    trial_border = today - timedelta(days=TRIAL_DAYS)
    quality_border = _minus_months(now_utc, 2)
    added = 0

    def add(child_id: int, reason: str) -> None:
        nonlocal added
        db.add(QmCall(child_id=child_id, reason=reason, status="new", source="auto", created_at=now_utc))
        open_ids.add(child_id)
        added += 1

    for child in children:
        if not _eligible(child):
            continue
        anchor = anchors.get(child.id)
        status = child.crm_status or ""
        if initial:
            # Первый запуск: все клиенты, у которых пробное было 2+ недели назад
            # (или занятий в системе нет) — «Контроль качества».
            if (anchor is not None and anchor <= trial_border) or (anchor is None and status == CLIENT):
                child.qm_trial_call_done = True
                if status == CLIENT and child.id not in open_ids:
                    add(child.id, "quality")
            continue
        # Учитываются только клиенты (CRM-статус «Клиент»)
        if status != CLIENT:
            continue
        # 2 недели после пробного (один раз на ученика — отметка в CRM)
        if not child.qm_trial_call_done and anchor is not None and anchor <= trial_border:
            child.qm_trial_call_done = True
            if child.id not in open_ids:
                add(child.id, "trial_2w")
            continue
        # Контроль качества: клиенты, которым звонили 2+ месяца назад
        if child.id in open_ids:
            continue
        done_at = last_done.get(child.id)
        if (done_at is not None and done_at <= quality_border) or (done_at is None and child.qm_trial_call_done):
            add(child.id, "quality")
    return added


async def refresh_regularity(db: AsyncSession) -> int:
    """Свежая выгрузка: клиенты без проведённых занятий 7+ дней."""
    today = _now_minsk().date()
    border = today - timedelta(days=INACTIVE_DAYS)
    children = await _children(db)
    last = await _last_completed(db)
    rows = (await db.execute(select(QmRegularity))).scalars().all()
    by_child: dict[int, list[QmRegularity]] = {}
    for r in rows:
        by_child.setdefault(r.child_id, []).append(r)
    added = 0
    for child in children:
        cid = child.id
        last_date = last.get(cid)
        rows_c = by_child.get(cid, [])
        still_open = False
        for r in rows_c:
            if r.status == "closed":
                continue
            if child.crm_status != CLIENT:
                await db.delete(r)  # больше не клиент — из «Регулярности» убираем
            elif last_date is not None and (r.last_lesson_date is None or last_date > r.last_lesson_date):
                await db.delete(r)  # ученик снова занимался — запись больше не нужна
            else:
                still_open = True
        if still_open or child.crm_status != CLIENT or not _eligible(child):
            continue
        if last_date is not None and last_date > border:
            continue
        # закрытую запись не открываем снова, пока после закрытия не было нового занятия
        if any((r.last_lesson_date or date.min) >= (last_date or date.min) for r in rows_c if r.status == "closed"):
            continue
        db.add(QmRegularity(child_id=cid, last_lesson_date=last_date, status="waiting", created_at=datetime.utcnow()))
        added += 1
    return added


async def nightly_regularity(db: AsyncSession) -> None:
    """После полуночи: проведено ли занятие к назначенной дате."""
    today = _now_minsk().date()
    last = await _last_completed(db)
    rows = (await db.execute(select(QmRegularity).where(QmRegularity.status != "closed"))).scalars().all()
    for r in rows:
        last_date = last.get(r.child_id)
        resumed = last_date is not None and last_date <= today and (r.last_lesson_date is None or last_date > r.last_lesson_date)
        if resumed:
            await db.delete(r)  # занятие проведено — запись закрывается сама
        else:
            r.overdue = bool(r.next_lesson_date and r.next_lesson_date < today)


JOBS = {
    "calls_refresh": _last_refresh_slot,
    "regularity_refresh": _last_refresh_slot,
    "regularity_nightly": _last_nightly_slot,
}


async def run_due_jobs(db: AsyncSession, force: bool = False) -> None:
    """Выполняет пропущенные плановые обновления (или все сразу при force)."""
    async with _lock:
        now = _now_minsk()
        jobs = {j.name: j for j in (await db.execute(select(QmJob))).scalars().all()}
        for name, slot_fn in JOBS.items():
            job = jobs.get(name)
            due_since = _to_utc_naive(slot_fn(now))
            if not force and job and job.last_run and job.last_run >= due_since:
                continue
            failed = _failed_at.get(name)
            if not force and failed and datetime.utcnow() - failed < timedelta(minutes=10):
                continue
            try:
                if name == "calls_refresh":
                    added = await refresh_calls(db, initial=not (job and job.last_run))
                    logger.info("QM calls refresh: %s new", added)
                elif name == "regularity_refresh":
                    added = await refresh_regularity(db)
                    logger.info("QM regularity refresh: %s new", added)
                else:
                    await nightly_regularity(db)
                if not job:
                    job = QmJob(name=name)
                    db.add(job)
                    jobs[name] = job
                job.last_run = datetime.utcnow()
                await db.commit()
                _failed_at.pop(name, None)
            except Exception:
                await db.rollback()
                _failed_at[name] = datetime.utcnow()
                logger.exception("QM job %s failed", name)
                jobs = {j.name: j for j in (await db.execute(select(QmJob))).scalars().all()}


async def qm_scheduler_task() -> None:
    """Фоновая проверка каждые 10 минут (пока сервер не спит)."""
    from app.db.session import async_session_maker

    await asyncio.sleep(60)
    while True:
        try:
            async with async_session_maker() as db:
                await run_due_jobs(db)
        except Exception:
            logger.exception("QM scheduler iteration failed")
        await asyncio.sleep(600)


# ─── данные для страниц ───────────────────────────────────────────────────────

async def _contacts(db: AsyncSession, child_ids: list[int]) -> dict[int, list[dict]]:
    if not child_ids:
        return {}
    res = await db.execute(
        select(ParentChild.child_id, User.last_name, User.first_name, User.middle_name, User.phone)
        .join(ParentProfile, ParentProfile.id == ParentChild.parent_id)
        .join(User, User.id == ParentProfile.user_id)
        .where(ParentChild.child_id.in_(child_ids))
    )
    out: dict[int, list[dict]] = {}
    for cid, last, first, middle, phone in res.all():
        out.setdefault(cid, []).append({
            "name": " ".join(x for x in (last, first, middle) if x).strip(),
            "phone": _real_phone(phone),
        })
    return out


async def _tutors(db: AsyncSession, child_ids: list[int]) -> dict[int, list[str]]:
    if not child_ids:
        return {}
    res = await db.execute(
        select(Lesson.child_id, User.last_name, User.first_name)
        .join(TutorProfile, TutorProfile.id == Lesson.tutor_id)
        .join(User, User.id == TutorProfile.user_id)
        .where(Lesson.child_id.in_(child_ids))
        .distinct()
    )
    out: dict[int, list[str]] = {}
    for cid, last, first in res.all():
        n = f"{last or ''} {first or ''}".strip()
        if n and n not in out.setdefault(cid, []):
            out[cid].append(n)
    return out


def _meta() -> dict:
    now = _now_minsk()
    return {"next_refresh": _next_refresh_slot(now).isoformat(), "today": now.date().isoformat()}


async def _job_times(db: AsyncSession) -> dict:
    res = await db.execute(select(QmJob))
    return {j.name: (j.last_run.isoformat() + "Z") if j.last_run else None for j in res.scalars().all()}


def _call_dict(c: QmCall, contacts: dict, anchors: dict) -> dict:
    child = c.child
    return {
        "id": c.id,
        "child_id": c.child_id,
        "student_name": _name(child.user if child else None),
        "crm_status": child.crm_status if child else None,
        "reason": c.reason,
        "reason_label": REASONS.get(c.reason, c.reason),
        "status": c.status,
        "feedback": c.feedback,
        "comment": c.comment or "",
        "source": c.source,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "closed_at": c.closed_at.isoformat() if c.closed_at else None,
        "trial_date": anchors.get(c.child_id).isoformat() if anchors.get(c.child_id) else None,
        "contacts": contacts.get(c.child_id, []),
    }


async def _load_call(db: AsyncSession, call_id: int) -> Optional[QmCall]:
    res = await db.execute(
        select(QmCall).where(QmCall.id == call_id).options(joinedload(QmCall.child).joinedload(ChildProfile.user))
    )
    return res.scalars().unique().one_or_none()


async def _one_call(db: AsyncSession, call_id: int) -> dict:
    c = await _load_call(db, call_id)
    if not c:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return _call_dict(c, await _contacts(db, [c.child_id]), await _anchor_dates(db, [c.child_id]))


# ─── API: звонки ──────────────────────────────────────────────────────────────

@router.get("/calls", dependencies=[Depends(require_admin)])
async def list_calls(view: str = Query("new"), db: AsyncSession = Depends(get_db)):
    await run_due_jobs(db)
    status = "done" if view == "all" else "new"
    q = select(QmCall).where(QmCall.status == status).options(joinedload(QmCall.child).joinedload(ChildProfile.user))
    q = q.order_by(QmCall.closed_at.desc(), QmCall.id.desc()) if status == "done" else q.order_by(QmCall.created_at.asc(), QmCall.id.asc())
    calls = (await db.execute(q)).scalars().unique().all()
    ids = list({c.child_id for c in calls})
    contacts = await _contacts(db, ids)
    anchors = await _anchor_dates(db)
    counts = dict((await db.execute(select(QmCall.status, func.count(QmCall.id)).group_by(QmCall.status))).all())
    return {
        "items": [_call_dict(c, contacts, anchors) for c in calls],
        "counts": {"new": counts.get("new", 0), "all": counts.get("done", 0)},
        "jobs": await _job_times(db),
        **_meta(),
    }


class CallCreate(BaseModel):
    child_id: int
    reason: str = "quality"


class CallUpdate(BaseModel):
    reason: Optional[str] = None
    feedback: Optional[str] = None
    comment: Optional[str] = None
    done: Optional[bool] = None
    clear_feedback: bool = False


@router.post("/calls", dependencies=[Depends(require_admin)], status_code=201)
async def create_call(body: CallCreate, db: AsyncSession = Depends(get_db)):
    if body.reason not in REASONS:
        raise HTTPException(status_code=400, detail="Неизвестный повод")
    child = await db.scalar(select(ChildProfile).where(ChildProfile.id == body.child_id))
    if not child:
        raise HTTPException(status_code=404, detail="Ученик не найден")
    exists = await db.scalar(select(QmCall.id).where(QmCall.child_id == body.child_id, QmCall.status == "new"))
    if exists:
        raise HTTPException(status_code=400, detail="Этому ученику уже запланирован звонок во вкладке «New»")
    call = QmCall(child_id=body.child_id, reason=body.reason, status="new", source="manual", created_at=datetime.utcnow())
    if body.reason == "trial_2w":
        child.qm_trial_call_done = True
    db.add(call)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Этому ученику уже запланирован звонок во вкладке «New»")
    return await _one_call(db, call.id)


@router.patch("/calls/{call_id}", dependencies=[Depends(require_admin)])
async def update_call(call_id: int, body: CallUpdate, db: AsyncSession = Depends(get_db)):
    call = await db.scalar(select(QmCall).where(QmCall.id == call_id))
    if not call:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if body.reason is not None:
        if body.reason not in REASONS:
            raise HTTPException(status_code=400, detail="Неизвестный повод")
        call.reason = body.reason
    if body.clear_feedback:
        call.feedback = None
    elif body.feedback is not None:
        if body.feedback not in FEEDBACK:
            raise HTTPException(status_code=400, detail="Неизвестная оценка")
        call.feedback = body.feedback
    if body.comment is not None:
        call.comment = body.comment.strip()[:4000] or None
    if body.done is not None:
        if body.done and call.status != "done":
            call.status, call.closed_at = "done", datetime.utcnow()
        elif not body.done and call.status == "done":
            other = await db.scalar(
                select(QmCall.id).where(QmCall.child_id == call.child_id, QmCall.status == "new", QmCall.id != call.id)
            )
            if other:
                raise HTTPException(status_code=400, detail="У ученика уже есть запись во вкладке «New»")
            call.status, call.closed_at = "new", None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="У ученика уже есть запись во вкладке «New»")
    return await _one_call(db, call_id)


@router.delete("/calls/{call_id}", dependencies=[Depends(require_admin)], status_code=204)
async def delete_call(call_id: int, db: AsyncSession = Depends(get_db)):
    """Убрать запись. Запись не стирается, а помечается «убрана» — чтобы
    система не добавила её снова при ближайшем обновлении (следующий плановый
    звонок — через 2 месяца)."""
    call = await db.scalar(select(QmCall).where(QmCall.id == call_id))
    if not call:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    call.status = "dismissed"
    call.closed_at = call.closed_at or datetime.utcnow()
    await db.commit()


@router.get("/students", dependencies=[Depends(require_admin)])
async def students_for_search(db: AsyncSession = Depends(get_db)):
    """Список учеников для поиска при добавлении звонка вручную."""
    children = await _children(db)
    rows = [{"child_id": c.id, "name": _name(c.user), "crm_status": c.crm_status} for c in children if c.user]
    rows.sort(key=lambda r: r["name"].lower())
    return rows


# ─── API: регулярность ────────────────────────────────────────────────────────

def _reg_dict(r: QmRegularity, tutors: dict, contacts: dict) -> dict:
    today = _now_minsk().date()
    child = r.child
    return {
        "id": r.id,
        "child_id": r.child_id,
        "student_name": _name(child.user if child else None),
        "tutors": tutors.get(r.child_id, []),
        "contacts": contacts.get(r.child_id, []),
        "last_lesson_date": r.last_lesson_date.isoformat() if r.last_lesson_date else None,
        "days_without": (today - r.last_lesson_date).days if r.last_lesson_date else None,
        "next_lesson_date": r.next_lesson_date.isoformat() if r.next_lesson_date else None,
        "status": r.status,
        "overdue": bool(r.overdue),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    }


@router.get("/regularity", dependencies=[Depends(require_admin)])
async def list_regularity(show_closed: bool = Query(False), db: AsyncSession = Depends(get_db)):
    await run_due_jobs(db)
    q = select(QmRegularity).options(joinedload(QmRegularity.child).joinedload(ChildProfile.user))
    if not show_closed:
        q = q.where(QmRegularity.status != "closed")
    rows = (await db.execute(q)).scalars().unique().all()
    ids = list({r.child_id for r in rows})
    tutors = await _tutors(db, ids)
    contacts = await _contacts(db, ids)
    items = [_reg_dict(r, tutors, contacts) for r in rows]
    items.sort(key=lambda x: (x["status"] == "closed", not x["overdue"], x["last_lesson_date"] or "0000"))
    closed_count = await db.scalar(select(func.count(QmRegularity.id)).where(QmRegularity.status == "closed"))
    return {"items": items, "closed_count": closed_count or 0, "jobs": await _job_times(db), **_meta()}


class RegUpdate(BaseModel):
    next_lesson_date: Optional[date] = None
    clear_next_date: bool = False
    status: Optional[str] = None


@router.patch("/regularity/{row_id}", dependencies=[Depends(require_admin)])
async def update_regularity(row_id: int, body: RegUpdate, db: AsyncSession = Depends(get_db)):
    r = await db.scalar(select(QmRegularity).where(QmRegularity.id == row_id))
    if not r:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    today = _now_minsk().date()
    if body.clear_next_date:
        r.next_lesson_date = None
        r.overdue = False
    elif body.next_lesson_date is not None:
        r.next_lesson_date = body.next_lesson_date
        r.overdue = body.next_lesson_date < today
    if body.status is not None:
        if body.status not in REG_STATUSES:
            raise HTTPException(status_code=400, detail="Неизвестный статус")
        if body.status == "closed" and r.status != "closed":
            r.closed_at = datetime.utcnow()
        elif body.status != "closed":
            r.closed_at = None
        r.status = body.status
    await db.commit()
    res = await db.execute(
        select(QmRegularity).where(QmRegularity.id == row_id).options(joinedload(QmRegularity.child).joinedload(ChildProfile.user))
    )
    row = res.scalars().unique().one()
    return _reg_dict(row, await _tutors(db, [row.child_id]), await _contacts(db, [row.child_id]))


@router.post("/refresh", dependencies=[Depends(require_admin)])
async def refresh_now(db: AsyncSession = Depends(get_db)):
    """Кнопка «Обновить сейчас»: выполнить все проверки немедленно."""
    await run_due_jobs(db, force=True)
    return {"ok": True, "jobs": await _job_times(db), **_meta()}


# ─── API: отказы ──────────────────────────────────────────────────────────────

@router.get("/refusals", dependencies=[Depends(require_admin)])
async def list_refusals(db: AsyncSession = Depends(get_db)):
    """Ученики со статусом «Отказ» в CRM."""
    res = await db.execute(
        select(ChildProfile).where(ChildProfile.crm_status == "Отказ").options(joinedload(ChildProfile.user))
    )
    children = [c for c in res.scalars().unique().all() if c.user]
    ids = [c.id for c in children]
    tutors = await _tutors(db, ids)
    contacts = await _contacts(db, ids)
    last = await _last_completed(db)
    calls: dict[int, QmCall] = {}
    if ids:
        for c in (await db.execute(
            select(QmCall).where(QmCall.child_id.in_(ids), QmCall.status == "done").order_by(QmCall.closed_at.asc())
        )).scalars().all():
            calls[c.child_id] = c  # последний совершённый звонок
    items = []
    for c in children:
        call = calls.get(c.id)
        items.append({
            "child_id": c.id,
            "student_name": _name(c.user),
            "tutors": tutors.get(c.id, []),
            "contacts": contacts.get(c.id, []),
            "last_lesson_date": last[c.id].isoformat() if last.get(c.id) else None,
            "reason": c.qm_refusal_reason or "",
            "crm_notes": getattr(c, "notes", None) or "",
            "last_call": {
                "feedback": call.feedback,
                "comment": call.comment or "",
                "closed_at": call.closed_at.isoformat() if call.closed_at else None,
                "reason_label": REASONS.get(call.reason, call.reason),
            } if call else None,
        })
    items.sort(key=lambda x: (x["last_lesson_date"] or "0000"), reverse=True)
    return {"items": items}


class RefusalUpdate(BaseModel):
    reason: str = ""


@router.patch("/refusals/{child_id}", dependencies=[Depends(require_admin)])
async def update_refusal(child_id: int, body: RefusalUpdate, db: AsyncSession = Depends(get_db)):
    child = await db.scalar(select(ChildProfile).where(ChildProfile.id == child_id))
    if not child:
        raise HTTPException(status_code=404, detail="Ученик не найден")
    child.qm_refusal_reason = body.reason.strip()[:4000] or None
    await db.commit()
    return {"child_id": child_id, "reason": child.qm_refusal_reason or ""}
