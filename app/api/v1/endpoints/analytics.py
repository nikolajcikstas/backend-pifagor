"""Аналитика для администратора.

Термины:
  • проведённое занятие — статус «Проведено» или прошедшее «Пробное»;
  • обычное занятие — со статусом «Проведено» (не «Пробное»): для коэффициента
    частотности, ставки репетитора и среднего чека.

Фактический коэффициент частотности (K) считается по календарным неделям
(пн–вс) и только по «устоявшимся» клиентам недели. Ученик входит в расчёт
недели W, если:
  А. его первое обычное занятие было раньше недели W (неделя старта и всё,
     что до неё, не учитывается — ритм ещё не установился);
  Б. он не ушёл: если сейчас статус «Отказ»/«Не занимаются» (или карточка
     отключена) — учитывается только до недели последнего занятия, не включая её;
  В. в течение недели W у него не было статуса «Отказ»/«Не занимаются»
     (история статусов + ночные снимки);
  Г. в CRM указан план «в неделю» (план — на конец недели W по ночному снимку,
     для недель до снимков — нынешний).
Состав учеников фиксирован на всю неделю. K(W) = обычные занятия этих учеников
за неделю ÷ их число. План той же недели — среднее поле «в неделю» тех же
учеников. За месяц/год — сумма занятий за полные недели ÷ сумма учеников по
неделям; неделя относится к месяцу, в котором её четверг (там больше её дней).
Незаконченная неделя не показывается; неделя с неотмеченными прошедшими
занятиями помечается как предварительная.
"""
from bisect import bisect_right
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
import logging

from sqlalchemy import select, func, and_, or_, event, inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import object_session

from app.core.deps import require_admin
from app.db.session import get_db
from app.models.models import (
    ChildProfile, User, Lesson, LessonStatus, TutorProfile, TutorRateHistory, EmailReceipt,
    AnalyticsDaily, CrmStatusEvent, AnalyticsClientDay, FreqIssue, AnalyticsArchive,
)
from app.api.v1.endpoints.quality import _now_minsk

router = APIRouter(prefix="/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

CLIENT = "Клиент"
LEFT_STATUSES = ("Отказ", "Не занимаются")
PROXY_DAYS = 28
# С этой даты «пришёл» = добавлен в CRM (дата создания карточки ученика).
# Раньше — дата первого проведённого занятия (данные переносились в систему).
ARRIVAL_BY_CRM_FROM = date(2026, 10, 1)
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
        if value == CLIENT and not getattr(target, "ref_code", None):
            # реферальный код выдаётся, когда ученик становится клиентом
            from app.services.referrals import new_code
            target.ref_code = new_code()
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
    """Пробное — только занятие со статусом «Пробное». Занятие со статусом
    «Проведено» считается проведённым, даже если было отмечено как пробное."""
    return Lesson.status == LessonStatus.trial


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
    rows = [{"day": today, "child_id": cid, "status": st, "plan": plan} for cid, st, plan in (await db.execute(
        select(ChildProfile.id, ChildProfile.crm_status, ChildProfile.lessons_per_week)
    )).all()]
    for i in range(0, len(rows), 500):
        ins = pg_insert(AnalyticsClientDay).values(rows[i:i + 500])
        await db.execute(ins.on_conflict_do_update(
            index_elements=["day", "child_id"], set_={"status": ins.excluded.status, "plan": ins.excluded.plan}))
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


# ─── частотность: «устоявшиеся» клиенты недели ────────────────────────────────

def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _minsk_date(dt: datetime) -> date:
    return (dt + timedelta(hours=3)).date()


class _FreqCalc:
    """Кто из учеников входит в расчёт каждой недели и сколько у него занятий.
    Все правила — в описании модуля (условия А–Г)."""

    def __init__(self, today: date):
        self.today = today
        self.kids: dict[int, tuple] = {}            # cid -> (status, plan сейчас, is_active)
        self.first_reg: dict[int, date] = {}
        self.last_reg: dict[int, date] = {}
        self.first_trial: dict[int, date] = {}
        self.left_spans: dict[int, list] = defaultdict(list)  # периоды в статусе «Отказ»/«Не занимаются»
        self.plan_snaps: dict[int, tuple] = {}  # cid -> ([дни], [планы]) по возрастанию
        self.les: dict[tuple, int] = defaultdict(int)          # (cid, пн) -> занятий
        self.hrs: dict[tuple, float] = defaultdict(float)
        self.unmarked: dict[date, int] = defaultdict(int)      # пн -> неотмеченных прошедших занятий

    def left_now(self, cid: int) -> bool:
        k = self.kids.get(cid)
        return bool(k) and (k[0] in LEFT_STATUSES or k[2] is False)

    def plan_at(self, cid: int, w: date):
        snaps = self.plan_snaps.get(cid)
        if snaps:
            i = bisect_right(snaps[0], w + timedelta(days=6))
            if i:
                return snaps[1][i - 1]
        k = self.kids.get(cid)
        return k[1] if k else None

    def _in_left_status(self, cid: int, w: date) -> bool:
        end = w + timedelta(days=6)
        for s_, e_ in self.left_spans.get(cid, ()):
            if s_ <= end and (e_ is None or e_ >= w):
                return True
        return False

    def state(self, cid: int, w: date) -> tuple[Optional[str], Optional[int]]:
        """in — в расчёте; new — новичок (неделя старта или ещё не начал после
        пробного); left — неделя ухода; break — был в статусе «Отказ»/«Не занимаются»;
        no_plan — нет плана в CRM; None — ещё не пришёл или давно ушёл."""
        fr = self.first_reg.get(cid)
        if self.left_now(cid):
            lr = self.last_reg.get(cid)
            if lr is None:
                return None, None
            if w > _monday(lr):
                return None, None
            if w == _monday(lr) and fr is not None and _monday(fr) < w:
                return "left", None
        if fr is None or _monday(fr) >= w:
            if fr is not None and _monday(fr) == w:
                return "new", self.plan_at(cid, w)
            ft = self.first_trial.get(cid)
            if ft is not None and ft <= w + timedelta(days=6) and ft >= w - timedelta(days=21):
                return "new", self.plan_at(cid, w)
            return None, None
        if self._in_left_status(cid, w):
            return "break", None
        plan = self.plan_at(cid, w)
        if plan is None or plan <= 0:
            return "no_plan", None
        return "in", plan

    def week(self, w: date) -> dict:
        n = lessons = plan_sum = 0
        hours = 0.0
        cnt = defaultdict(int)
        for cid in self.kids:
            st, plan = self.state(cid, w)
            if st is None:
                continue
            cnt[st] += 1
            if st == "in":
                n += 1
                plan_sum += plan
                lessons += self.les.get((cid, w), 0)
                hours += self.hrs.get((cid, w), 0.0)
        return {"week": w, "n": n, "lessons": lessons, "hours": hours, "plan_sum": plan_sum,
                "newcomers": cnt["new"], "left": cnt["left"] + cnt["break"], "no_plan": cnt["no_plan"],
                "unmarked": self.unmarked.get(w, 0), "complete": w + timedelta(days=6) < self.today}


def _freq_agg(weeks: list[dict]) -> dict:
    """Сводка по нескольким полным неделям: Σ занятий ÷ Σ учеников по неделям."""
    weeks = [x for x in weeks if x["complete"]]
    n = sum(x["n"] for x in weeks)
    lessons = sum(x["lessons"] for x in weeks)
    hours = sum(x["hours"] for x in weeks)
    plan = sum(x["plan_sum"] for x in weeks)
    return {
        "weeks": len(weeks),
        "from": min(x["week"] for x in weeks).isoformat() if weeks else None,
        "to": (max(x["week"] for x in weeks) + timedelta(days=6)).isoformat() if weeks else None,
        "n_avg": round(n / len(weeks), 1) if weeks else None,
        "lessons": lessons, "hours": round(hours, 1),
        "k": round(lessons / n, 2) if n else None,
        "k_hours": round(hours / n, 2) if n else None,
        "plan_k": round(plan / n, 2) if n else None,
        "fulfil_pct": round(100 * lessons / plan, 1) if plan else None,
        "newcomers": sum(x["newcomers"] for x in weeks),
        "left": sum(x["left"] for x in weeks),
        "no_plan": sum(x["no_plan"] for x in weeks),
        "unmarked": sum(x["unmarked"] for x in weeks),
        "preliminary": any(x["unmarked"] for x in weeks),
    }


async def _freq_calc(db: AsyncSession, w_from: date, w_to: date, today: date) -> _FreqCalc:
    w_from, w_to = _monday(w_from), _monday(w_to)
    d_from, d_to = w_from, w_to + timedelta(days=6)
    c = _FreqCalc(today)
    for cid, st, plan, active in (await db.execute(
        select(ChildProfile.id, ChildProfile.crm_status, ChildProfile.lessons_per_week, User.is_active)
        .join(User, User.id == ChildProfile.user_id)
    )).all():
        c.kids[cid] = (st, plan, active)
    for cid, mn, mx in (await db.execute(
        select(Lesson.child_id, func.min(Lesson.date), func.max(Lesson.date))
        .where(Lesson.status == LessonStatus.completed, Lesson.date <= today).group_by(Lesson.child_id)
    )).all():
        c.first_reg[cid], c.last_reg[cid] = mn, mx
    for cid, mn in (await db.execute(
        select(Lesson.child_id, func.min(Lesson.date)).where(_is_trial(), Lesson.date <= today).group_by(Lesson.child_id)
    )).all():
        c.first_trial[cid] = mn
    for cid, d, h in (await db.execute(
        select(Lesson.child_id, Lesson.date, _hours_expr())
        .where(Lesson.status == LessonStatus.completed, Lesson.date >= d_from, Lesson.date <= min(d_to, today))
    )).all():
        c.les[(cid, _monday(d))] += 1
        c.hrs[(cid, _monday(d))] += float(h or 0)
    for d, n in (await db.execute(
        select(Lesson.date, func.count(Lesson.id))
        .where(Lesson.status == LessonStatus.scheduled, Lesson.date >= d_from, Lesson.date < min(d_to + timedelta(days=1), today))
        .group_by(Lesson.date)
    )).all():
        c.unmarked[_monday(d)] += int(n)
    # периоды в статусе «Отказ»/«Не занимаются» — по истории смены статуса
    evs = (await db.execute(
        select(CrmStatusEvent.child_id, CrmStatusEvent.old_status, CrmStatusEvent.new_status, CrmStatusEvent.changed_at)
        .order_by(CrmStatusEvent.child_id, CrmStatusEvent.changed_at, CrmStatusEvent.id)
    )).all()
    track_from = min((_minsk_date(e[3]) for e in evs), default=None)
    by_child = defaultdict(list)
    for e in evs:
        by_child[e[0]].append(e)
    for cid, lst in by_child.items():
        if lst[0][1] in LEFT_STATUSES and track_from:
            c.left_spans[cid].append((track_from, _minsk_date(lst[0][3])))
        for i, e in enumerate(lst):
            if e[2] in LEFT_STATUSES:
                c.left_spans[cid].append((_minsk_date(e[3]), _minsk_date(lst[i + 1][3]) if i + 1 < len(lst) else None))
    # ночные снимки: план на конец недели и статус по дням
    for cid, d, st, plan in (await db.execute(
        select(AnalyticsClientDay.child_id, AnalyticsClientDay.day, AnalyticsClientDay.status, AnalyticsClientDay.plan)
        .where(AnalyticsClientDay.day >= d_from - timedelta(days=60), AnalyticsClientDay.day <= d_to)
        .order_by(AnalyticsClientDay.day)
    )).all():
        sn = c.plan_snaps.setdefault(cid, ([], []))
        sn[0].append(d)
        sn[1].append(plan)
        if st in LEFT_STATUSES:
            c.left_spans[cid].append((d, d))
    return c


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


async def _summary(db: AsyncSession, start: date, end: date, today: date, rate=None, fq: Optional[dict] = None) -> dict:
    """Итоговые показатели за период."""
    last = min(end, today)
    if last < start:
        return {"empty": True}
    held = and_(_held(today), Lesson.date >= start, Lesson.date <= end)
    regular = and_(held, ~_is_trial())
    students = await db.scalar(select(func.count(func.distinct(Lesson.child_id))).where(held)) or 0
    lessons, hours = (await db.execute(select(func.count(Lesson.id), func.coalesce(func.sum(_hours_expr()), 0)).where(regular))).one()
    trials = await db.scalar(select(func.count(Lesson.id)).where(held, _is_trial())) or 0
    fq = fq or {}
    # ставка репетитора и средний чек — по обычным проведённым занятиям
    rate = rate or await _rates(db)
    # средний чек — по цене ученика, действовавшей на дату занятия (история цен)
    from app.services.pricing import load_prices
    book = await load_prices(db)
    price_sum = 0.0
    for cid, d, cnt in (await db.execute(
        select(Lesson.child_id, Lesson.date, func.count(Lesson.id)).where(regular).group_by(Lesson.child_id, Lesson.date)
    )).all():
        price_sum += book.price(cid, d) * cnt
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
        "freq_k": fq.get("k"),
        "freq_k_hours": fq.get("k_hours"),
        "freq_plan_k": fq.get("plan_k"),
        "freq_n": fq.get("n_avg"),
        "freq_weeks": fq.get("weeks", 0),
        "freq_from": fq.get("from"),
        "freq_to": fq.get("to"),
        "freq_preliminary": fq.get("preliminary", False),
        "avg_rate": round(rate_sum / lessons, 2) if lessons else None,
        "avg_check": round(price_sum / lessons, 2) if lessons else None,
        "paid_total": round(paid, 2),
        "paid_per_lesson": round(paid / lessons, 2) if lessons else None,
        "revenue": round(price_sum, 2),
        "tutor_cost": round(rate_sum, 2),
    }


# ─── архив итогов ─────────────────────────────────────────────────────────────
# Через ARCHIVE_SETTLE_DAYS дней после окончания периода (успели отметить
# занятия и поставить статусы) его итоги записываются в analytics_archive и
# дальше берутся оттуда. Удаление учеников/репетиторов, правки задним числом и
# смена формул не меняют прошлые графики.

ARCHIVE_SETTLE_DAYS = 14
ARCHIVE_VERSION = 1


def _settled_until(today: date) -> date:
    """Последний день, итоги по которому уже закрепляются."""
    return today - timedelta(days=ARCHIVE_SETTLE_DAYS + 1)


def _weeks_in(a: date, b: date) -> list[date]:
    """Недели (пн), четверг которых попадает в [a, b]."""
    out, w = [], _monday(a) - timedelta(days=7)
    while w <= b:
        if a <= w + timedelta(days=3) <= b:
            out.append(w)
        w += timedelta(days=7)
    return out


def _drange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _json_default(x):
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if isinstance(x, set):
        return sorted(x)
    raise TypeError(type(x))


async def _arch_load(db: AsyncSession, kind: str, keys) -> dict[str, dict]:
    keys = sorted(set(keys))
    out: dict[str, dict] = {}
    for i in range(0, len(keys), 500):
        for k, data in (await db.execute(
            select(AnalyticsArchive.key, AnalyticsArchive.data)
            .where(AnalyticsArchive.kind == kind, AnalyticsArchive.key.in_(keys[i:i + 500]))
        )).all():
            out[k] = json.loads(data)
    return out


async def _day_facts(db: AsyncSession, a: date, b: date, today: date, rate=None) -> dict[date, dict]:
    """По дням [a, b]: ученики на проведённых занятиях, занятия, пробные и
    нагрузка репетиторов по статусам (занятия, часы, ученики, пробные, сумма ставок)."""
    out = {d: {"students": set(), "lessons": 0, "trials": 0, "tutors": {}, "tn": {}} for d in _drange(a, b)}
    for cid, d, is_tr in (await db.execute(
        select(Lesson.child_id, Lesson.date, _is_trial()).where(_held(today), Lesson.date >= a, Lesson.date <= b)
    )).all():
        x = out[d]
        x["students"].add(cid)
        x["lessons"] += 1
        if is_tr:
            x["trials"] += 1
    rate = rate or await _rates(db)
    names = {tid: f"{ln or ''} {fn or ''}".strip() for tid, ln, fn in (await db.execute(
        select(TutorProfile.id, User.last_name, User.first_name).join(User, User.id == TutorProfile.user_id)
    )).all()}
    for tid, cid, d, st, is_tr, h in (await db.execute(
        select(Lesson.tutor_id, Lesson.child_id, Lesson.date, Lesson.status, _is_trial(), _hours_expr())
        .where(Lesson.date >= a, Lesson.date <= b)
    )).all():
        x = out[d]
        t = x["tutors"].setdefault(str(tid), {})
        s = t.setdefault(st.value if hasattr(st, "value") else str(st), {"l": 0, "h": 0.0, "c": [], "t": 0, "rs": 0.0})
        s["l"] += 1
        s["h"] += float(h or 0)
        if cid not in s["c"]:
            s["c"].append(cid)
        if is_tr:
            s["t"] += 1
        elif st == LessonStatus.completed:
            s["rs"] += rate(tid, d)
        if tid in names:
            x["tn"][str(tid)] = names[tid]
    return out


async def _came_left_by_day(db: AsyncSession, today: date) -> tuple[dict, dict, dict]:
    """Пришли (и из них сейчас «Клиент») и ушли — по дням, за всё время.
    Пришёл: с 01.10.2026 — дата добавления в CRM, раньше — первое занятие.
    Ушёл: статус «Отказ»/«Не занимаются» — дата смены статуса, для старых — последнее занятие."""
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
    came, conv, left = defaultdict(int), defaultdict(int), defaultdict(int)
    for cid, status, is_active, created_at in (await db.execute(
        select(ChildProfile.id, ChildProfile.crm_status, User.is_active, User.created_at).join(User, User.id == ChildProfile.user_id)
    )).all():
        added = (created_at + timedelta(hours=3)).date() if created_at else None
        f = added if (added is not None and added >= ARRIVAL_BY_CRM_FROM) else first_lesson.get(cid)
        if f:
            came[f] += 1
            if status == CLIENT:
                conv[f] += 1
        if status in LEFT_STATUSES or is_active is False:
            ev = left_events.get(cid)
            ld = (ev + timedelta(hours=3)).date() if ev else last_lesson.get(cid)
            if ld:
                left[ld] += 1
    return came, conv, left


async def freeze_settled(db: AsyncSession) -> int:
    """Закрепить итоги всех прошедших периодов, которые ещё не в архиве.
    Первый запуск заполняет архив за всю историю. Возвращает число записей."""
    today = _now_minsk().date()
    su = _settled_until(today)
    first = await db.scalar(select(func.min(Lesson.date)))
    if not first or first > su:
        return 0
    have = {(k, key) for k, key in (await db.execute(select(AnalyticsArchive.kind, AnalyticsArchive.key))).all()}
    rows: list[tuple[str, str, dict]] = []
    rate = await _rates(db)

    need_days = [d for d in _drange(first, su) if ("day", d.isoformat()) not in have]
    if need_days:
        facts = await _day_facts(db, need_days[0], need_days[-1], today, rate)
        came, conv, left = await _came_left_by_day(db, today)
        active, _, _ = await _active_by_day(db, need_days[0], need_days[-1], today)
        for d in need_days:
            f = facts[d]
            rows.append(("day", d.isoformat(), {
                "students": len(f["students"]), "lessons": f["lessons"], "trials": f["trials"],
                "tutors": f["tutors"], "tn": f["tn"],
                "came": came.get(d, 0), "came_clients": conv.get(d, 0), "left": left.get(d, 0),
                "active": active.get(d),
            }))

    need_weeks = []
    w = _monday(first)
    while w + timedelta(days=6) <= su:
        if ("week", w.isoformat()) not in have:
            need_weeks.append(w)
        w += timedelta(days=7)
    months, years = [], []
    m = date(first.year, first.month, 1)
    while True:
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        if nxt - timedelta(days=1) > su:
            break
        if ("month", m.isoformat()[:7]) not in have:
            months.append((m, nxt - timedelta(days=1)))
        m = nxt
    for y in range(first.year, su.year + 1):
        if date(y, 12, 31) <= su and ("year", str(y)) not in have:
            years.append((date(y, 1, 1), date(y, 12, 31)))
    wk_needed = set(need_weeks)
    for a, b in months + years:
        wk_needed.update(_weeks_in(a, b))
    wk: dict[date, dict] = {}
    if wk_needed:
        calc = await _freq_calc(db, min(wk_needed), max(wk_needed), today)
        for x in wk_needed:
            wk[x] = calc.week(x)
    for w in need_weeks:
        fq = _freq_agg([wk[w]])
        rows.append(("week", w.isoformat(), {"k": wk[w], "summary": await _summary(db, w, w + timedelta(days=6), today, rate, fq)}))
    for kind, periods in (("month", months), ("year", years)):
        for a, b in periods:
            fq = _freq_agg([wk[x] for x in _weeks_in(a, b)])
            s = await _summary(db, a, b, today, rate, fq)
            rows.append((kind, a.isoformat()[:7] if kind == "month" else str(a.year), {
                "summary": s, "h_students": s.get("students", 0),
                "h_lessons": s.get("lessons", 0) + s.get("trials", 0), "h_trials": s.get("trials", 0),
            }))
    if rows:
        now = datetime.utcnow()
        for i in range(0, len(rows), 200):
            vals = [{"kind": k, "key": key, "frozen_at": now,
                     "data": json.dumps({**data, "v": ARCHIVE_VERSION}, default=_json_default, ensure_ascii=False)}
                    for k, key, data in rows[i:i + 200]]
            await db.execute(pg_insert(AnalyticsArchive).values(vals).on_conflict_do_nothing(index_elements=["kind", "key"]))
        await db.commit()
    return len(rows)


def _arch_week(x: dict) -> dict:
    k = dict(x["k"])
    k["week"] = date.fromisoformat(k["week"])
    k["complete"] = True
    return k


# ─── основной отчёт ───────────────────────────────────────────────────────────

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
    su = _settled_until(today)

    # 1. ученики по дням / месяцам / годам + скользящее среднее
    #    (дни — за 7 дней, месяцы — за 3 месяца; только завершённые дни/месяцы)
    sb = _buckets(kind_students, start, end)
    ma_n = {"day": 7, "month": 3}.get(kind_students, 0)
    if kind_students == "day":
        q_from = start - timedelta(days=6)
    elif kind_students == "month":
        m0 = start.year * 12 + start.month - 1 - 2
        q_from = date(m0 // 12, m0 % 12 + 1, 1)
    else:
        q_from = start
    pre = _buckets(kind_students, q_from, start - timedelta(days=1)) if q_from < start else []
    allb = pre + sb
    arch_kind = {"day": "day", "month": "month", "year": "year"}[kind_students]
    arch_key = (lambda b: b["start"].isoformat()) if arch_kind == "day" else \
        (lambda b: b["start"].isoformat()[:7]) if arch_kind == "month" else (lambda b: str(b["start"].year))
    frozen_b = [b for b in allb if b["end"] <= su]
    arch = await _arch_load(db, arch_kind, [arch_key(b) for b in frozen_b])
    apos = _bucket_of(allb)
    sets = [set() for _ in allb]
    les = [0] * len(allb)
    trl = [0] * len(allb)
    for cid, d, is_tr in (await db.execute(
        select(Lesson.child_id, Lesson.date, _is_trial()).where(_held(today), Lesson.date >= q_from, Lesson.date <= end)
    )).all():
        i = apos(d)
        if i is not None:
            sets[i].add(cid)
            les[i] += 1
            if is_tr:
                trl[i] += 1
    cnt = [len(s) for s in sets]
    for i, b in enumerate(allb):
        a_ = arch.get(arch_key(b)) if b["end"] <= su else None
        if a_ is not None:  # закреплённые итоги
            if arch_kind == "day":
                cnt[i], les[i], trl[i] = a_["students"], a_["lessons"], a_["trials"]
            else:
                cnt[i], les[i], trl[i] = a_["h_students"], a_["h_lessons"], a_["h_trials"]
    students_series = []
    for i, b in enumerate(sb):
        j = len(pre) + i
        ma = None
        if ma_n and b["end"] < today and j + 1 >= ma_n:
            ma = round(sum(cnt[k] for k in range(j - ma_n + 1, j + 1)) / ma_n, 1)
        students_series.append({"key": b["key"], "label": b["label"], "date": b["start"].isoformat(),
                                "wd": b["start"].weekday(), "students": cnt[j], "lessons": les[j],
                                "trials": trl[j], "ma": ma, "future": b["start"] > today})

    # 2. коэффициент частотности: полные календарные недели, «устоявшиеся» клиенты
    if grain == "week":
        w_sel = _monday(start)
        groups = [(w.isoformat(), f"{w.strftime('%d.%m')}–{(w + timedelta(days=6)).strftime('%d.%m')}", [w])
                  for w in (w_sel - timedelta(weeks=k) for k in range(11, -1, -1))]
        f_unit = "week"
    elif grain == "month":
        # недели месяца + 4 недели до него (для сравнения; в итог месяца не входят)
        mw = _weeks_in(start, end)
        groups = [(w.isoformat(), f"{w.strftime('%d.%m')}–{(w + timedelta(days=6)).strftime('%d.%m')}", [w])
                  for w in [mw[0] - timedelta(weeks=k) for k in (4, 3, 2, 1)] + mw]
        f_unit = "week"
    else:
        groups = [(b["key"], b["label"], _weeks_in(b["start"], b["end"])) for b in _buckets("month", start, end)]
        f_unit = "month"
    p_start = p_end = None
    partial = start <= today < end
    if grain != "all":
        p_start, p_end, _ = _period(grain, start - timedelta(days=1), first_day, today)
        if partial:
            p_end = min(p_end, p_start + (today - start))
    # последняя полная неделя периода (для недели — из показанных 12 недель)
    cands = [w for w in ([w for g in groups for w in g[2]] if grain == "week" else _weeks_in(start, end))
             if w + timedelta(days=6) < today]
    lw = max(cands) if cands else None
    all_weeks = sorted(set([w for g in groups for w in g[2]] + _weeks_in(start, end)
                           + (_weeks_in(p_start, p_end) if p_start else [])))
    arch_w = await _arch_load(db, "week", [w.isoformat() for w in all_weeks if w + timedelta(days=6) <= su])
    live_weeks = [w for w in all_weeks if w.isoformat() not in arch_w]
    calc = await _freq_calc(db, min(live_weeks), max(live_weeks), today) if live_weeks else None
    wk_cache: dict[date, dict] = {}

    def wk(w: date) -> dict:
        if w not in wk_cache:
            a_ = arch_w.get(w.isoformat())
            wk_cache[w] = _arch_week(a_) if a_ is not None else calc.week(w)
        return wk_cache[w]

    freq_series = []
    for key, lbl, ws in groups:
        if not ws:
            continue
        ag = _freq_agg([wk(w) for w in ws])
        if not ag["weeks"]:
            continue  # незаконченная неделя/месяц без полных недель — не показываем
        ag.update({"key": key, "label": lbl, "partial": ag["weeks"] < len(ws),
                   "in_period": any(start <= w + timedelta(days=3) <= end for w in ws)})
        freq_series.append(ag)
    while freq_series and not freq_series[0]["n_avg"]:
        freq_series.pop(0)  # недели до первых клиентов не показываем
    fq_cur = _freq_agg([wk(w) for w in _weeks_in(start, end)])
    fq_prev = _freq_agg([wk(w) for w in _weeks_in(p_start, p_end)]) if p_start else {}
    x = wk(lw) if lw else None
    last_week = None
    if x and x["complete"]:
        last_week = {"week_start": lw.isoformat(),
                     "label": f"{lw.strftime('%d.%m')}–{(lw + timedelta(days=6)).strftime('%d.%m')}",
                     **{k: x[k] for k in ("n", "lessons", "plan_sum", "newcomers", "left", "no_plan", "unmarked")}}

    # 5. динамика: пришли / ушли (по дням; закреплённые дни — из архива)
    db_b = _buckets(kind_dyn, start, end)
    dpos = _bucket_of(db_b)
    arch_d = await _arch_load(db, "day", [d.isoformat() for d in _drange(start, min(end, su))])
    l_came, l_conv, l_left = await _came_left_by_day(db, today)
    came = [0] * len(db_b)
    left = [0] * len(db_b)
    converted = 0
    for d in _drange(start, end):
        i = dpos(d)
        if i is None:
            continue
        a_ = arch_d.get(d.isoformat()) if d <= su else None
        if a_ is not None:
            c, cc, lf = a_["came"], a_["came_clients"], a_["left"]
        else:
            c, cc, lf = l_came.get(d, 0), l_conv.get(d, 0), l_left.get(d, 0)
        came[i] += c
        converted += cc
        left[i] += lf
    a0 = arch_d.get(start.isoformat()) if start <= su else None
    if a0 is not None:
        base_active = a0.get("active")
    else:
        base_active = (await _active_by_day(db, start, start, today))[0].get(start) if start <= today else None
    total_came, total_left = sum(came), sum(left)
    dynamics = {
        "series": [{"key": b["key"], "label": b["label"], "came": came[i], "left": left[i], "future": b["start"] > today}
                   for i, b in enumerate(db_b)],
        "came": total_came, "left": total_left, "net": total_came - total_left,
        "base_active": base_active,
        "churn_pct": round(100 * total_left / base_active, 1) if base_active else None,
        "conversion_pct": round(100 * converted / total_came, 1) if total_came else None,
        "crm_from": ARRIVAL_BY_CRM_FROM.isoformat(),
    }

    # занятия не по 60 минут (кроме отменённых) — чтобы находить ошибки во времени
    dur_min = func.round(func.extract("epoch", Lesson.time_end - Lesson.time_start) / 60)
    odd_rows = (await db.execute(
        select(Lesson.child_id, Lesson.tutor_id, Lesson.date, Lesson.time_start, Lesson.time_end, Lesson.status, dur_min)
        .where(Lesson.date >= start, Lesson.date <= end, Lesson.status != LessonStatus.cancelled, dur_min != 60)
        .order_by(Lesson.date.desc())
    )).all()
    odd = {}
    if odd_rows:
        names = {cid: f"{ln or ''} {fn or ''}".strip() for cid, ln, fn in (await db.execute(
            select(ChildProfile.id, User.last_name, User.first_name).join(User, User.id == ChildProfile.user_id)
            .where(ChildProfile.id.in_({r[0] for r in odd_rows}))
        )).all()}
        tnames = {tid: f"{ln or ''} {fn or ''}".strip() for tid, ln, fn in (await db.execute(
            select(TutorProfile.id, User.last_name, User.first_name).join(User, User.id == TutorProfile.user_id)
        )).all()}
        for cid, tid, d, ts, te, st, m in odd_rows:
            o = odd.setdefault(cid, {"child_id": cid, "name": names.get(cid, ""), "tutors": [], "count": 0, "durations": {}, "lessons": []})
            o["count"] += 1
            mm = int(m or 0)
            o["durations"][mm] = o["durations"].get(mm, 0) + 1
            tn = tnames.get(tid)
            if tn and tn not in o["tutors"]:
                o["tutors"].append(tn)
            if len(o["lessons"]) < 5:
                o["lessons"].append({"date": d.isoformat(), "time": f"{ts.strftime('%H:%M')}–{te.strftime('%H:%M')}", "minutes": mm})
    odd_list = sorted(odd.values(), key=lambda o: (-o["count"], o["name"]))
    for o in odd_list:
        o["durations"] = [{"minutes": k, "count": v} for k, v in sorted(o["durations"].items())]

    # итоги и сравнение с прошлым периодом (закреплённые периоды — из архива)
    def arch_ref(g: str, a: date) -> Optional[tuple[str, str]]:
        return {"week": ("week", a.isoformat()), "month": ("month", a.isoformat()[:7]), "year": ("year", str(a.year))}.get(g)

    async def period_summary(a: date, b: date, fq: dict, full: bool) -> dict:
        ref = arch_ref(grain, a)
        if full and ref and b <= su:
            got = await _arch_load(db, ref[0], [ref[1]])
            if ref[1] in got:
                return dict(got[ref[1]]["summary"])
        return await _summary(db, a, b, today, rate, fq)

    rate = await _rates(db)
    summary = await period_summary(start, end, fq_cur, True)
    if grain == "all":
        prev = None
    else:
        p_full_end = _period(grain, start - timedelta(days=1), first_day, today)[1]
        prev = await period_summary(p_start, p_end, fq_prev, p_end == p_full_end)
        prev["label"] = _period(grain, start - timedelta(days=1), first_day, today)[2]
        prev["partial"] = partial

    return {
        "grain": grain, "label": label, "start": start.isoformat(), "end": end.isoformat(), "today": today.isoformat(),
        "settled_until": su.isoformat(),
        "summary": summary, "previous": prev,
        "students_series": students_series,
        "freq": {"series": freq_series, "unit": f_unit, "last_week": last_week},
        "dynamics": dynamics,
        "odd_durations": odd_list,
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
    месяц — по неделям, год — по месяцам. Закреплённые дни — из архива."""
    today = _now_minsk().date()
    anchor = anchor or today
    first_day = await _first_day(db, today)
    start, end, label = _period(grain, anchor, first_day, today)
    su = _settled_until(today)
    kind = GRAIN_BUCKETS[grain][1]
    buckets = _buckets(kind, start, end)
    pos = _bucket_of(buckets)
    wanted = [s for s in statuses.split(",") if s in STATUS_KEYS] or ["completed", "trial"]
    arch = await _arch_load(db, "day", [d.isoformat() for d in _drange(start, min(end, su))])
    live_days = [d for d in _drange(start, end) if d.isoformat() not in arch]
    live = await _day_facts(db, live_days[0], live_days[-1], today) if live_days else {}
    names = {tid: f"{ln or ''} {fn or ''}".strip() for tid, ln, fn in (await db.execute(
        select(TutorProfile.id, User.last_name, User.first_name).join(User, User.id == TutorProfile.user_id)
    )).all()}
    per = defaultdict(lambda: {"lessons": [0] * len(buckets), "hours": [0.0] * len(buckets),
                               "students": [set() for _ in buckets], "all_students": set(),
                               "trials": 0, "rate_sum": 0.0, "rate_n": 0, "name": None})
    for d in _drange(start, end):
        i = pos(d)
        if i is None:
            continue
        day = arch.get(d.isoformat())
        if day is None:
            day = live.get(d)
            if day is None:
                continue
        for tid_s, sts in day["tutors"].items():
            tid = int(tid_s)
            t = per[tid]
            if not t["name"]:
                t["name"] = day.get("tn", {}).get(tid_s)
            for st in wanted:
                s = sts.get(st)
                if not s:
                    continue
                t["lessons"][i] += s["l"]
                t["hours"][i] += s["h"]
                t["students"][i].update(s["c"])
                t["all_students"].update(s["c"])
                t["trials"] += s["t"]
                if st == "completed":
                    t["rate_sum"] += s["rs"]
                    t["rate_n"] += s["l"] - s["t"]
    tutors = []
    for tid, t in per.items():
        if not sum(t["lessons"]):
            continue
        tutors.append({
            "id": tid, "name": names.get(tid) or t["name"] or f"Репетитор #{tid}",
            "lessons": t["lessons"], "hours": [round(x, 1) for x in t["hours"]],
            "students": [len(s) for s in t["students"]],
            "total": {"lessons": sum(t["lessons"]), "hours": round(sum(t["hours"]), 1),
                      "students": len(t["all_students"]), "trials": t["trials"]},
            "avg_rate": round(t["rate_sum"] / t["rate_n"], 2) if t["rate_n"] else None,
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
        debt = float(r.debt)  # проведено по ценам на даты занятий − оплачено
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

def _user_label(u: User) -> str:
    return f"{u.last_name or ''} {u.first_name or ''}".strip() or (u.email or "")


def _issue_dict(x: Optional[FreqIssue]) -> Optional[dict]:
    if not x:
        return None
    return {
        "week_start": x.week_start.isoformat(),
        "comment": x.comment, "comment_by": x.comment_by,
        "comment_at": x.comment_at.isoformat() + "Z" if x.comment_at else None,
        "resolved": x.resolved_at is not None,
        "resolved_at": x.resolved_at.isoformat() + "Z" if x.resolved_at else None,
        "resolved_by": x.resolved_by,
    }


@router.get("/students-frequency", dependencies=[Depends(require_admin)])
async def students_frequency(week: Optional[date] = Query(None), db: AsyncSession = Depends(get_db)):
    """Клиенты: план из CRM («В неделю») и факт — проведённые (не пробные)
    занятия за неделю. Правила те же, что у коэффициента частотности: новичок
    учитывается со следующей недели после первого проведённого занятия, недобор
    считается только у учеников «в расчёте». Среднее за 4 недели — по неделям,
    где ученик был в расчёте."""
    today = _now_minsk().date()
    if week is None:
        week = _monday(today) - timedelta(days=7)  # прошлая полная неделя
    start = _monday(week)
    end = start + timedelta(days=6)
    w4 = [start - timedelta(weeks=k) for k in (3, 2, 1, 0)]
    calc = await _freq_calc(db, w4[0], start, today)
    children = (await db.execute(
        select(ChildProfile.id, User.last_name, User.first_name)
        .join(User, User.id == ChildProfile.user_id)
        .where(ChildProfile.crm_status == CLIENT, User.is_active.isnot(False))
    )).all()
    tutors = defaultdict(list)
    for cid, ln, fn in (await db.execute(
        select(Lesson.child_id, User.last_name, User.first_name)
        .join(TutorProfile, TutorProfile.id == Lesson.tutor_id).join(User, User.id == TutorProfile.user_id)
        .where(Lesson.date >= w4[0], Lesson.date <= end + timedelta(days=28)).distinct()
    )).all():
        n = f"{ln or ''} {fn or ''}".strip()
        if n and n not in tutors[cid]:
            tutors[cid].append(n)
    issues = defaultdict(list)
    ids = [c[0] for c in children]
    if ids:
        for x in (await db.execute(
            select(FreqIssue).where(FreqIssue.child_id.in_(ids), FreqIssue.week_start <= start)
            .order_by(FreqIssue.week_start.desc())
        )).scalars().all():
            issues[x.child_id].append(x)
    items = []
    for cid, ln, fn in children:
        st, plan = calc.state(cid, start)
        fact = calc.les.get((cid, start), 0)
        elig = [w for w in w4 if calc.state(cid, w)[0] == "in"]
        avg4 = round(sum(calc.les.get((cid, w), 0) for w in elig) / len(elig), 2) if elig else None
        gap = (plan - fact) if st == "in" else None
        cur = next((x for x in issues[cid] if x.week_start == start), None)
        prev = [x for x in issues[cid] if x.week_start < start]
        resolved_before = [x for x in prev if x.resolved_at is not None]
        repeat_n = len(resolved_before) + 1 if (resolved_before and gap is not None and gap > 0) else 0
        items.append({
            "child_id": cid, "name": f"{ln or ''} {fn or ''}".strip(),
            "state": st or "none", "plan": plan if st == "in" else calc.plan_at(cid, start),
            "fact_week": fact, "fact_avg4": avg4, "weeks_in4": len(elig),
            "gap": gap, "tutors": tutors.get(cid, []),
            "issue": _issue_dict(cur), "repeat_n": repeat_n,
            "history": [_issue_dict(x) for x in prev if x.comment or x.resolved_at][:10],
        })
    items.sort(key=lambda x: (-(x["gap"] if x["gap"] is not None else -99), x["name"]))
    in_calc = [x for x in items if x["state"] == "in"]
    under = [x for x in in_calc if x["gap"] > 0 and not (x["issue"] and x["issue"]["resolved"])]
    wk_info = calc.week(start)
    return {
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        "label": f"{start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}",
        "is_complete": end < today,
        "unmarked": wk_info["unmarked"],
        "items": items,
        "under_count": len(under),
        "hidden_count": sum(1 for x in items if x["issue"] and x["issue"]["resolved"]),
        "new_count": sum(1 for x in items if x["state"] == "new"),
        "no_plan_count": sum(1 for x in items if x["state"] == "no_plan"),
        "in_count": len(in_calc),
        "plan_total": sum(x["plan"] for x in in_calc),
        "fact_total": sum(x["fact_week"] for x in in_calc),
    }


class FreqIssueIn(BaseModel):
    child_id: int
    week_start: date
    comment: Optional[str] = None
    resolved: Optional[bool] = None


@router.put("/freq-issues")
async def save_freq_issue(body: FreqIssueIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    """Комментарий менеджера к недобору ученика за неделю и/или отметка «Разобрано»."""
    if body.comment is not None and len(body.comment) > 2000:
        raise HTTPException(status_code=400, detail="Комментарий слишком длинный (до 2000 символов)")
    if not await db.get(ChildProfile, body.child_id):
        raise HTTPException(status_code=404, detail="Ученик не найден")
    ws = _monday(body.week_start)
    now, who = datetime.utcnow(), _user_label(user)
    for attempt in (1, 2):
        x = (await db.execute(
            select(FreqIssue).where(FreqIssue.child_id == body.child_id, FreqIssue.week_start == ws)
        )).scalars().first()
        if x is None:
            x = FreqIssue(child_id=body.child_id, week_start=ws)
            db.add(x)
        if body.comment is not None:
            text_ = body.comment.strip() or None
            if text_ != x.comment:
                x.comment, x.comment_by, x.comment_at = text_, who, now
        if body.resolved is not None:
            if body.resolved and x.resolved_at is None:
                x.resolved_at, x.resolved_by = now, who
            elif not body.resolved:
                x.resolved_at, x.resolved_by = None, None
        try:
            await db.commit()
            break
        except IntegrityError:  # запись за эту неделю успели создать параллельно
            await db.rollback()
            if attempt == 2:
                raise HTTPException(status_code=409, detail="Не удалось сохранить, попробуйте ещё раз")
    await db.refresh(x)
    return _issue_dict(x)
