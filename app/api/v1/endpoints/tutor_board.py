"""Дашборд договоров репетиторов (вкладка «Репетиторы»): предмет, действие
по договору, дата окончания, статус договора, комментарий."""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_admin
from app.db.session import get_db
from app.models.models import Subject, TutorContractInfo, TutorProfile, TutorSubject, User
from app.services.pricing import minsk_today

router = APIRouter(prefix="/tutor-board", tags=["tutor-board"])

ACTIONS = ["Активный — ничего не нужно", "Продлить", "Расторгнуть", "Пока не нужен", "Заключить"]
SOON_DAYS = 30


def contract_status(action: Optional[str], end: Optional[date], today: date) -> dict:
    """Статус договора по действию и дате окончания.
    • «Расторгнуть» — «Нужно расторгнуть»;
    • дата окончания не наступила — «Действует» (за 30 дней — с пометкой, что скоро истекает);
    • дата прошла — «Истёк»;
    • даты нет — «Нет договора»."""
    if action == "Расторгнуть":
        return {"status": "Нужно расторгнуть", "tone": "red", "days_left": (end - today).days if end else None}
    if end is None:
        return {"status": "Нет договора", "tone": "amber", "days_left": None}
    left = (end - today).days
    if left < 0:
        return {"status": "Истёк", "tone": "red", "days_left": left}
    return {"status": "Действует", "tone": "green" if left > SOON_DAYS else "soon", "days_left": left}


def _row(tid, ln, fn, active, subjects, info: Optional[TutorContractInfo], today: date) -> dict:
    action = info.action if info else None
    end = info.end_date if info else None
    return {"tutor_id": tid, "name": f"{ln or ''} {fn or ''}".strip() or f"#{tid}", "active": active is not False,
            "subjects": subjects, "action": action, "end_date": end.isoformat() if end else None,
            "comment": info.comment if info else None,
            "updated_at": info.updated_at.isoformat() + "Z" if info and info.updated_at else None,
            "updated_by": info.updated_by if info else None,
            **contract_status(action, end, today)}


@router.get("", dependencies=[Depends(require_admin)])
async def tutor_board(include_inactive: bool = Query(False), db: AsyncSession = Depends(get_db)):
    today = minsk_today()
    subj: dict[int, list[str]] = {}
    for tid, name in (await db.execute(
        select(TutorSubject.tutor_id, Subject.name).join(Subject, Subject.id == TutorSubject.subject_id)
        .order_by(Subject.name)
    )).all():
        if name and name not in subj.setdefault(tid, []):
            subj[tid].append(name)
    infos = {x.tutor_id: x for x in (await db.execute(select(TutorContractInfo))).scalars().all()}
    q = select(TutorProfile.id, User.last_name, User.first_name, User.is_active).join(User, User.id == TutorProfile.user_id)
    if not include_inactive:
        q = q.where(User.is_active.isnot(False))
    rows = [_row(tid, ln, fn, act, subj.get(tid, []), infos.get(tid), today)
            for tid, ln, fn, act in (await db.execute(q.order_by(User.last_name, User.first_name))).all()]
    return {"rows": rows, "actions": ACTIONS, "today": today.isoformat(), "soon_days": SOON_DAYS}


class BoardIn(BaseModel):
    action: Optional[str] = Field(None, max_length=60)
    end_date: Optional[date] = None
    comment: Optional[str] = Field(None, max_length=2000)


@router.put("/{tutor_id}")
async def update_tutor_board(tutor_id: int, body: BoardIn, db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_admin)):
    """Меняются только присланные поля; null — очистить."""
    tutor = await db.get(TutorProfile, tutor_id)
    if not tutor:
        raise HTTPException(status_code=404, detail="Репетитор не найден")
    sent = body.model_fields_set
    if "action" in sent and body.action not in (None, "") and body.action not in ACTIONS:
        raise HTTPException(status_code=400, detail="Неизвестное действие по договору")
    if "end_date" in sent and body.end_date and not (date(2000, 1, 1) <= body.end_date <= date(2100, 12, 31)):
        raise HTTPException(status_code=400, detail="Проверьте дату окончания договора")
    info = await db.get(TutorContractInfo, tutor_id)
    if info is None:
        info = TutorContractInfo(tutor_id=tutor_id)
        db.add(info)
    if "action" in sent:
        info.action = body.action or None
    if "end_date" in sent:
        info.end_date = body.end_date
    if "comment" in sent:
        info.comment = (body.comment or "").strip() or None
    info.updated_at = datetime.utcnow()
    info.updated_by = (f"{user.last_name or ''} {user.first_name or ''}".strip() or user.email)[:200]
    await db.commit()
    u = await db.get(User, tutor.user_id)
    subjects = [n for (n,) in (await db.execute(
        select(Subject.name).join(TutorSubject, TutorSubject.subject_id == Subject.id)
        .where(TutorSubject.tutor_id == tutor_id).order_by(Subject.name)
    )).all()]
    return _row(tutor_id, u.last_name if u else "", u.first_name if u else "", u.is_active if u else True,
                subjects, info, minsk_today())
