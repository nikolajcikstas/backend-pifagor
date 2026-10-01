"""Аналитика для администратора.

Термины:
  • проведённое занятие — статус «Проведено» или прошедшее «Пробное»;
  • обычное занятие — проведённое и не пробное (для коэффициента частотности,
    ставки репетитора и среднего чека);
  • активные клиенты за день N(d) — сколько учеников в этот день были в CRM
    со статусом «Клиент». Система сохраняет это число каждый день (таблица
    analytics_daily). Для дней до запуска снимков используется оценка:
    ученики, у которых было проведённое занятие за 28 дней до этого дня.

Фактический коэффициент частотности за период:
    K = 7 × (обычных занятий за период) / Σ N(d) по дням периода
то есть «занятий на одного клиента в неделю», где каждый день учитывается
с тем числом клиентов, которое было именно в этот день (средневзвешенно).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
import logging

from sqlalchemy import select, func, and_, or_, event, inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import object_session

from app.core.deps import require_admin
from app.db.session import get_db
from app.models.models import (
    ChildProfile, User, Lesson, LessonStatus, TutorProfile, TutorRateHistory, EmailReceipt,
    AnalyticsDaily, CrmStatusEvent,
)
from app.api.v1.endpoints.quality import _now_minsk

router = APIRouter(prefix="/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

CLIENT = "Клиент"
LEFT_STATUSES = ("Отказ", "Не занимаются")
PROXY_DAYS = 28
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MONTHS_FULL = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ─── история статусов ─────────────────────────────────────────────────────────

@event.listens_for(ChildProfile.crm_status, "set")
def _log_status_change(target, value, oldvalue, initiator):
    """Любая смена CRM-статуса (карточка ученика, привязка договора и т.д.)
    записывается в историю автоматически."""
    try:
        st = sa_inspect(target)
        if st.key is None:  # новый ученик, ещё не сохранён — это не смена статуса
            return
        old = oldvalue if isinstance(oldvalue, str) else None
        if value == old:
            return
        sess = object_session(target)
        if sess is not None:
            sess.add(CrmStatusEvent(child_id=st.identity[0], old_status=old, new_status=value, changed_at=datetime.utcnow()))
    except Exception:  # история — вспомогательная, не должна мешать сохранению
        logger.exception("Не удалось записать смену CRM-статуса")


# ─── периоды ──────────────────────────────────────────────────────────────────

def _period(grain: str, anchor: date, first_day: date, today: date) -> tuple[date, date, str]:
    if grain == "week":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        label = f"{start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}"
    elif grain == "year":
        start, end = date(anchor.year, 1, 1), date(anchor.year, 12, 31)
        label = str(anchor.year)
    elif grain == "all":
        start, end = date(first_day.year, 1, 1), date(today.year, 12, 31)
        label = "Всё время"
    else:
        start = date(anchor.year, anchor.month, 1)
        nxt = date(anchor.year + (anchor.month == 12), anchor.month % 12 + 1, 1)
        end = nxt - timedelta(days=1)
        label = f"{MONTHS_FULL[anchor.month - 1]} {anchor.year}"
    return start, end, label


def _buckets(kind: str, start: date, end: date) -> list[dict]:
    """kind: day | week | month | year → [{key, label, start, end}]"""
    out = []
    if kind == "day":
        d = start
        while d <= end:
            out.append({"key": d.isoformat(), "label": f"{WD[d.weekday()]} {d.day}" if (end - start).days <= 6 else str(d.day),
                        "start": d, "end": d})
            d += timedelta(days=1)
    elif kind == "week":
        d = start - timedelta(days=start.weekday())
        while d <= end:
            e = d + timedelta(days=6)
            out.append({"key": d.isoformat(), "label": f"{max(d, start).strftime('%d.%m')}–{min(e, end).strftime('%d.%m')}",
                        "start": max(d, start), "end": min(e, end)})
            d += timedelta(days=7)
    elif kind == "month":
        d = date(start.year, start.month, 1)
        while d <= end:
            nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            lbl = MONTHS[d.month - 1] + (f" {str(d.year)[2:]}" if start.year != end.year else "")
            out.append({"key": d.isoformat()[:7], "label": lbl, "start": max(d, start), "end": min(nxt - timedelta(days=1), end)})
            d = nxt
    else:
        for y in range(start.year, end.year + 1):
            out.append({"key": str(y), "label": str(y), "start": max(date(y, 1, 1), start), "end": min(date(y, 12, 31), end)})
    return out


def _bucket_of(buckets: list[dict]):
    index = {}
    for i, b in enumerate(buckets):
        d = b["start"]
        while d <= b["end"]:
            index[d] = i
            d += timedelta(days=1)
    return index.get


def _held(today: date):
    return and_(Lesson.date <= today, Lesson.status.in_([LessonStatus.completed, LessonStatus.trial]))


def _is_trial():
    return or_(Lesson.is_free_trial.is_(True), Lesson.status == LessonStatus.trial)


def _hours_expr():
    return func.greatest(func.extract("epoch", Lesson.time_end - Lesson.time_start) / 3600.0, 0)


# ─── снимки активных клиентов ─────────────────────────────────────────────────

async def _count_clients(db: AsyncSession) -> int:
    return int(await db.scalar(
        select(func.count(ChildProfile.id)).join(User, User.id == ChildProfile.user_id)
        .where(ChildProfile.crm_status == CLIENT, User.is_active.isnot(False))
    ) or 0)


async def save_today_snapshot(db: AsyncSession) -> None:
    """Записать число клиентов на сегодня (минское время). Вызывается ночью
    и при открытии аналитики; повторный вызов в тот же день обновляет значение."""
    today = _now_minsk().date()
    n = await _count_clients(db)
    await db.execute(
        pg_insert(AnalyticsDaily).values(day=today, active_clients=n, created_at=datetime.utcnow())
        .on_conflict_do_update(index_elements=["day"], set_={"active_clients": n})
    )
    await db.commit()


async def _active_by_day(db: AsyncSession, start: date, end: date, today: date) -> tuple[dict[date, float], Optional[date], set]:
    """N(d) для каждого дня [start, min(end, today)].
    • есть снимок за день — берём его;
    • снимка нет, но система уже их вела (сервер «спал») — берём последний
      предыдущий снимок;
    • день раньше первого снимка — оценка: ученики с проведённым занятием
      за 28 дней до этого дня.
    Возвращает (N по дням, дата первого снимка, дни с оценкой)."""
    last = min(end, today)
    out: dict[date, float] = {}
    estimated: set = set()
    if last < start:
        return out, await db.scalar(select(func.min(AnalyticsDaily.day))), estimated
    first_snapshot = await db.scalar(select(func.min(AnalyticsDaily.day)))
    snaps = {r.day: r.active_clients for r in (await db.execute(
        select(AnalyticsDaily).where(AnalyticsDaily.day >= start, AnalyticsDaily.day <= last)
    )).scalars().all()}
    carry = None
    if first_snapshot and first_snapshot < start:
        prev = (await db.execute(
            select(AnalyticsDaily).where(AnalyticsDaily.day < start).order_by(AnalyticsDaily.day.desc()).limit(1)
        )).scalars().first()
        carry = prev.active_clients if prev else None
    proxy_end = min(last, (first_snapshot - timedelta(days=1)) if first_snapshot else last)
    proxy: dict[date, int] = defaultdict(int)
    if proxy_end >= start:
        # ученик «активен» 28 дней после каждого проведённого занятия
        rows = (await db.execute(
            select(Lesson.child_id, Lesson.date).where(_held(today), Lesson.date >= start - timedelta(days=PROXY_DAYS), Lesson.date <= proxy_end)
            .order_by(Lesson.child_id, Lesson.date)
        )).all()
        by_child: dict[int, list[date]] = defaultdict(list)
        for cid, d in rows:
            by_child[cid].append(d)
        for dates in by_child.values():
            cur_s = cur_e = None
            for d in dates:
                s_, e_ = d, d + timedelta(days=PROXY_DAYS - 1)
                if cur_e is not None and s_ <= cur_e + timedelta(days=1):
                    cur_e = max(cur_e, e_)
                else:
                    if cur_s is not None:
                        _add_range(proxy, cur_s, cur_e, start, proxy_end)
                    cur_s, cur_e = s_, e_
            if cur_s is not None:
                _add_range(proxy, cur_s, cur_e, start, proxy_end)
    d = start
    while d <= last:
        if d in snaps:
            carry = snaps[d]
            out[d] = float(carry)
        elif first_snapshot and d >= first_snapshot and carry is not None:
            out[d] = float(carry)  # пропущенный день — как в последнем снимке
        else:
            out[d] = float(proxy.get(d, 0))
            estimated.add(d)
        d += timedelta(days=1)
    return out, first_snapshot, estimated


def _add_range(acc: dict, s: date, e: date, lo: date, hi: date) -> None:
    d, e = max(s, lo), min(e, hi)
    while d <= e:
        acc[d] += 1
        d += timedelta(days=1)


# ─── основной отчёт ───────────────────────────────────────────────────────────

GRAIN_BUCKETS = {
    # (гистограмма учеников, репетиторы/динамика)
    "week": ("day", "day"),
    "month": ("day", "week"),
    "year": ("month", "month"),
    "all": ("year", "year"),
}


async def _first_day(db: AsyncSession, today: date) -> date:
    return await db.scalar(select(func.min(Lesson.date))) or today


async def _rates(db: AsyncSession):
    hist = defaultdict(list)
    for h in (await db.execute(select(TutorRateHistory).order_by(TutorRateHistory.effective_from))).scalars().all():
        hist[h.tutor_id].append((h.effective_from, h.rate_per_hour))
    flat = dict((await db.execute(select(TutorProfile.id, TutorProfile.rate_per_hour))).all())

    def rate(tutor_id: int, d: date) -> float:
        hs = hist.get(tutor_id)
        if not hs:
            return float(flat.get(tutor_id) or 0)
        r = hs[0][1]
        for f, v in hs:
            if f <= d:
                r = v
            else:
                break
        return float(r or 0)
    return rate


async def _summary(db: AsyncSession, start: date, end: date, today: date, rate=None) -> dict:
    """Итоговые показатели за период."""
    last = min(end, today)
    if last < start:
        return {"empty": True}
    held = and_(_held(today), Lesson.date >= start, Lesson.date <= end)
    regular = and_(held, ~_is_trial())
    students = await db.scalar(select(func.count(func.distinct(Lesson.child_id))).where(held)) or 0
    lessons, hours = (await db.execute(select(func.count(Lesson.id), func.coalesce(func.sum(_hours_expr()), 0)).where(regular))).one()
    trials = await db.scalar(select(func.count(Lesson.id)).where(held, _is_trial())) or 0
    active, _, _ = await _active_by_day(db, start, end, today)
    person_days = sum(active.values())
    k = 7 * lessons / person_days if person_days else None
    kh = 7 * float(hours) / person_days if person_days else None
    # ставка репетитора и средний чек — по обычным проведённым занятиям
    rate = rate or await _rates(db)
    price_sum = float(await db.scalar(
        select(func.coalesce(func.sum(ChildProfile.lesson_price), 0)).select_from(Lesson)
        .join(ChildProfile, ChildProfile.id == Lesson.child_id).where(regular)
    ) or 0)
    # ставка зависит от репетитора и даты — считаем по группам (репетитор, день)
    rate_sum = 0.0
    for t, d, cnt in (await db.execute(
        select(Lesson.tutor_id, Lesson.date, func.count(Lesson.id)).where(regular).group_by(Lesson.tutor_id, Lesson.date)
    )).all():
        rate_sum += rate(t, d) * cnt
    paid = float(await db.scalar(
        select(func.coalesce(func.sum(EmailReceipt.amount), 0)).where(
            EmailReceipt.payment_date >= datetime.combine(start, datetime.min.time()),
            EmailReceipt.payment_date < datetime.combine(end + timedelta(days=1), datetime.min.time()))
    ) or 0)
    return {
        "students": int(students),
        "lessons": int(lessons),
        "trials": int(trials),
        "hours": round(float(hours), 1),
        "avg_clients": round(person_days / len(active), 1) if active else None,
        "freq_k": round(k, 2) if k is not None else None,
        "freq_k_hours": round(kh, 2) if kh is not None else None,
        "avg_rate": round(rate_sum / lessons, 2) if lessons else None,
        "avg_check": round(price_sum / lessons, 2) if lessons else None,
        "paid_total": round(paid, 2),
        "paid_per_lesson": round(paid / lessons, 2) if lessons else None,
        "revenue": round(price_sum, 2),
        "tutor_cost": round(rate_sum, 2),
    }


@router.get("/overview", dependencies=[Depends(require_admin)])
async def overview(
    grain: str = Query("month", pattern="^(week|month|year|all)$"),
    anchor: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    today = _now_minsk().date()
    if not await db.get(AnalyticsDaily, today):
        await save_today_snapshot(db)
    anchor = anchor or today
    first_day = await _first_day(db, today)
    start, end, label = _period(grain, anchor, first_day, today)
    kind_students, kind_dyn = GRAIN_BUCKETS[grain]

    # 1. ученики по дням / месяцам / годам
    sb = _buckets(kind_students, start, end)
    pos = _bucket_of(sb)
    held_rows = (await db.execute(
        select(Lesson.child_id, Lesson.date, _is_trial()).where(_held(today), Lesson.date >= start, Lesson.date <= end)
    )).all()
    sets = [set() for _ in sb]
    les = [0] * len(sb)
    for cid, d, _ in held_rows:
        i = pos(d)
        if i is not None:
            sets[i].add(cid)
            les[i] += 1
    students_series = [{"key": b["key"], "label": b["label"], "students": len(sets[i]), "lessons": les[i],
                        "future": b["start"] > today} for i, b in enumerate(sb)]

    # 2. коэффициент частотности по неделям (для года и «всё время» — по месяцам)
    fb = _buckets("week" if grain in ("week", "month") else "month", start if grain != "week" else start - timedelta(weeks=11), end)
    f_start = fb[0]["start"] if fb else start
    active, first_snapshot, est_days = await _active_by_day(db, f_start, end, today)
    reg_rows = (await db.execute(
        select(Lesson.date, _hours_expr()).where(_held(today), ~_is_trial(), Lesson.date >= f_start, Lesson.date <= end)
    )).all()
    fpos = _bucket_of(fb)
    f_les = [0] * len(fb)
    f_hours = [0.0] * len(fb)
    for d, h in reg_rows:
        i = fpos(d)
        if i is not None:
            f_les[i] += 1
            f_hours[i] += float(h or 0)
    freq_series = []
    for i, b in enumerate(fb):
        days = [b["start"] + timedelta(days=j) for j in range((b["end"] - b["start"]).days + 1)]
        pd = sum(active.get(d, 0) for d in days if d <= today)
        freq_series.append({
            "key": b["key"], "label": b["label"], "lessons": f_les[i], "hours": round(f_hours[i], 1),
            "avg_clients": round(pd / max(1, len([d for d in days if d <= today])), 1) if pd else None,
            "k": round(7 * f_les[i] / pd, 2) if pd else None,
            "k_hours": round(7 * f_hours[i] / pd, 2) if pd else None,
            "estimated": any(d in est_days for d in days),
            "partial": b["end"] > today,
        })
    while freq_series and not freq_series[0]["lessons"] and freq_series[0]["k"] in (None, 0):
        freq_series.pop(0)  # пустые месяцы до первого занятия не показываем
    plan = await db.scalar(
        select(func.avg(ChildProfile.lessons_per_week)).join(User, User.id == ChildProfile.user_id)
        .where(ChildProfile.crm_status == CLIENT, User.is_active.isnot(False), ChildProfile.lessons_per_week.isnot(None))
    )

    # 5. динамика: пришли / ушли
    db_b = _buckets(kind_dyn, start, end)
    dpos = _bucket_of(db_b)
    first_lesson = dict((await db.execute(
        select(Lesson.child_id, func.min(Lesson.date)).where(_held(today)).group_by(Lesson.child_id)
    )).all())
    last_lesson = dict((await db.execute(
        select(Lesson.child_id, func.max(Lesson.date)).where(_held(today)).group_by(Lesson.child_id)
    )).all())
    left_events = dict((await db.execute(
        select(CrmStatusEvent.child_id, func.max(CrmStatusEvent.changed_at))
        .where(CrmStatusEvent.new_status.in_(LEFT_STATUSES)).group_by(CrmStatusEvent.child_id)
    )).all())
    children = (await db.execute(
        select(ChildProfile.id, ChildProfile.crm_status, User.is_active).join(User, User.id == ChildProfile.user_id)
    )).all()
    came = [0] * len(db_b)
    left = [0] * len(db_b)
    came_ids, converted = [], 0
    for cid, status, is_active in children:
        f = first_lesson.get(cid)
        if f and start <= f <= end:
            i = dpos(f)
            if i is not None:
                came[i] += 1
                came_ids.append(cid)
                if status == CLIENT:
                    converted += 1
        if status in LEFT_STATUSES or is_active is False:
            ev = left_events.get(cid)
            ld = (ev + timedelta(hours=3)).date() if ev else last_lesson.get(cid)
            if ld and start <= ld <= end:
                i = dpos(ld)
                if i is not None:
                    left[i] += 1
    base_active = (await _active_by_day(db, start, start, today))[0].get(start) if start <= today else None
    total_came, total_left = sum(came), sum(left)
    dynamics = {
        "series": [{"key": b["key"], "label": b["label"], "came": came[i], "left": left[i], "future": b["start"] > today}
                   for i, b in enumerate(db_b)],
        "came": total_came, "left": total_left, "net": total_came - total_left,
        "base_active": base_active,
        "churn_pct": round(100 * total_left / base_active, 1) if base_active else None,
        "conversion_pct": round(100 * converted / total_came, 1) if total_came else None,
    }

    # итоги и сравнение с прошлым периодом
    rate = await _rates(db)
    summary = await _summary(db, start, end, today, rate)
    if grain == "all":
        prev = None
    else:
        p_anchor = start - timedelta(days=1)
        ps, pe, plabel = _period(grain, p_anchor, first_day, today)
        partial = start <= today < end
        if partial:
            # текущий период ещё идёт — сравниваем с тем же числом дней прошлого периода
            pe = min(pe, ps + (today - start))
        prev = await _summary(db, ps, pe, today, rate)
        prev["label"] = plabel
        prev["partial"] = partial

    return {
        "grain": grain, "label": label, "start": start.isoformat(), "end": end.isoformat(), "today": today.isoformat(),
        "summary": summary, "previous": prev,
        "students_series": students_series,
        "freq": {"series": freq_series, "plan_k": round(float(plan), 2) if plan is not None else None,
                 "first_snapshot": first_snapshot.isoformat() if first_snapshot else None},
        "dynamics": dynamics,
    }


# ─── нагрузка репетиторов ─────────────────────────────────────────────────────

STATUS_KEYS = {"completed", "trial", "scheduled", "cancelled", "rescheduled"}


@router.get("/tutors", dependencies=[Depends(require_admin)])
async def tutors_load(
    grain: str = Query("week", pattern="^(week|month|year|all)$"),
    anchor: Optional[date] = Query(None),
    statuses: str = Query("completed,trial"),
    db: AsyncSession = Depends(get_db),
):
    """Занятия, часы и ученики по каждому репетитору: неделя — по дням,
    месяц — по неделям, год — по месяцам."""
    today = _now_minsk().date()
    anchor = anchor or today
    first_day = await _first_day(db, today)
    start, end, label = _period(grain, anchor, first_day, today)
    kind = GRAIN_BUCKETS[grain][1]
    buckets = _buckets(kind, start, end)
    pos = _bucket_of(buckets)
    wanted = [s for s in statuses.split(",") if s in STATUS_KEYS] or ["completed", "trial"]
    rows = (await db.execute(
        select(Lesson.tutor_id, Lesson.child_id, Lesson.date, Lesson.status, _is_trial(), _hours_expr())
        .where(Lesson.date >= start, Lesson.date <= end, Lesson.status.in_([LessonStatus(s) for s in wanted]))
    )).all()
    names = {tid: f"{ln or ''} {fn or ''}".strip() for tid, ln, fn in (await db.execute(
        select(TutorProfile.id, User.last_name, User.first_name).join(User, User.id == TutorProfile.user_id)
    )).all()}
    rate = await _rates(db)
    per = defaultdict(lambda: {"lessons": [0] * len(buckets), "hours": [0.0] * len(buckets),
                               "students": [set() for _ in buckets], "all_students": set(),
                               "trials": 0, "rates": []})
    for tid, cid, d, st, is_trial, h in rows:
        i = pos(d)
        if i is None:
            continue
        t = per[tid]
        t["lessons"][i] += 1
        t["hours"][i] += float(h or 0)
        t["students"][i].add(cid)
        t["all_students"].add(cid)
        if is_trial:
            t["trials"] += 1
        elif st == LessonStatus.completed:
            t["rates"].append(rate(tid, d))
    tutors = []
    for tid, t in per.items():
        tutors.append({
            "id": tid, "name": names.get(tid) or f"Репетитор #{tid}",
            "lessons": t["lessons"], "hours": [round(x, 1) for x in t["hours"]],
            "students": [len(s) for s in t["students"]],
            "total": {"lessons": sum(t["lessons"]), "hours": round(sum(t["hours"]), 1),
                      "students": len(t["all_students"]), "trials": t["trials"]},
            "avg_rate": round(sum(t["rates"]) / len(t["rates"]), 2) if t["rates"] else None,
        })
    tutors.sort(key=lambda x: (-x["total"]["lessons"], x["name"]))
    return {
        "grain": grain, "label": label, "start": start.isoformat(), "end": end.isoformat(),
        "buckets": [{"key": b["key"], "label": b["label"]} for b in buckets],
        "statuses": wanted, "tutors": tutors,
    }


# ─── деньги: дебиторка и оплачено вперёд ──────────────────────────────────────

@router.get("/finance", dependencies=[Depends(require_admin)])
async def finance_state(db: AsyncSession = Depends(get_db)):
    """На сегодня, по тем же цифрам, что «Финансовый отчёт» в «Оплатах»:
    • дебиторка — проведено занятий × цена − оплачено (если больше нуля);
    • оплачено вперёд — (оплаченных занятий − проведённых) × цена занятия."""
    from app.services.finance_report import compute_finance_rows
    rows = await compute_finance_rows(db)
    debtors, ahead = [], []
    for r in rows:
        price = float(r.lesson_price or 0)
        debt = max(0.0, r.lessons_conducted * price - float(r.amount_paid or 0))
        extra = max(0, (r.lessons_paid or 0) - (r.lessons_conducted or 0))
        if debt > 0.009:
            debtors.append({"child_id": r.child_id, "name": r.student_name, "amount": round(debt, 2),
                            "lessons_conducted": r.lessons_conducted, "lessons_paid": r.lessons_paid, "price": price})
        if extra > 0 and price > 0:
            ahead.append({"child_id": r.child_id, "name": r.student_name, "amount": round(extra * price, 2),
                          "lessons_ahead": extra, "lessons_conducted": r.lessons_conducted, "lessons_paid": r.lessons_paid, "price": price})
    debtors.sort(key=lambda x: -x["amount"])
    ahead.sort(key=lambda x: -x["amount"])
    return {
        "debt_total": round(sum(x["amount"] for x in debtors), 2),
        "ahead_total": round(sum(x["amount"] for x in ahead), 2),
        "ahead_lessons": sum(x["lessons_ahead"] for x in ahead),
        "debtors": debtors, "ahead": ahead,
    }


# ─── план/факт частотности по ученикам ────────────────────────────────────────

@router.get("/students-frequency", dependencies=[Depends(require_admin)])
async def students_frequency(week: Optional[date] = Query(None), db: AsyncSession = Depends(get_db)):
    """Клиенты: план из CRM («В неделю») и факт — проведённые (не пробные)
    занятия за выбранную неделю и в среднем за 4 недели по эту неделю."""
    today = _now_minsk().date()
    if week is None:
        week = today - timedelta(days=today.weekday() + 7)  # прошлая полная неделя
    start = week - timedelta(days=week.weekday())
    end = start + timedelta(days=6)
    start4 = start - timedelta(weeks=3)
    children = (await db.execute(
        select(ChildProfile.id, ChildProfile.lessons_per_week, User.last_name, User.first_name)
        .join(User, User.id == ChildProfile.user_id)
        .where(ChildProfile.crm_status == CLIENT, User.is_active.isnot(False))
    )).all()
    counts = defaultdict(lambda: [0, 0])
    for cid, d in (await db.execute(
        select(Lesson.child_id, Lesson.date).where(
            Lesson.status == LessonStatus.completed, ~_is_trial(), Lesson.date >= start4, Lesson.date <= end)
    )).all():
        counts[cid][1] += 1
        if d >= start:
            counts[cid][0] += 1
    tutors = defaultdict(list)
    for cid, ln, fn in (await db.execute(
        select(Lesson.child_id, User.last_name, User.first_name)
        .join(TutorProfile, TutorProfile.id == Lesson.tutor_id).join(User, User.id == TutorProfile.user_id)
        .where(Lesson.date >= start4, Lesson.date <= end + timedelta(days=28)).distinct()
    )).all():
        n = f"{ln or ''} {fn or ''}".strip()
        if n and n not in tutors[cid]:
            tutors[cid].append(n)
    items = []
    for cid, plan, ln, fn in children:
        week_fact, four = counts[cid]
        items.append({
            "child_id": cid, "name": f"{ln or ''} {fn or ''}".strip(), "plan": plan,
            "fact_week": week_fact, "fact_avg4": round(four / 4, 2),
            "gap": (plan - week_fact) if plan is not None else None,
            "tutors": tutors.get(cid, []),
        })
    items.sort(key=lambda x: (-(x["gap"] if x["gap"] is not None else -99), x["name"]))
    under = [x for x in items if x["gap"] is not None and x["gap"] > 0]
    return {
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        "label": f"{start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}",
        "is_complete": end < today,
        "items": items,
        "under_count": len(under),
        "no_plan_count": sum(1 for x in items if x["plan"] is None),
        "plan_total": sum(x["plan"] or 0 for x in items),
        "fact_total": sum(x["fact_week"] for x in items if x["plan"] is not None),
    }
