"""Учёт посещений личного кабинета родителями и учениками.

Что считается:
  • session  — сеанс: открыл кабинет. Новый сеанс — если до этого 30 минут
               не было никаких действий (общепринятое правило веб-аналитики);
  • payments — родитель открыл «Оплаты»;
  • schedule — на вкладке «Расписание» на экране оказался список занятий
               (нижний блок), а не только календарь;
  • reports  — родитель пробыл на вкладке «Отчёты» не меньше 5 секунд
               (а не «зашёл и вышел»);
  • homework — ученик открыл «Домашнее задание».
Один и тот же раздел у одного человека считается не чаще раза в 5 минут
(переключения туда-обратно не раздувают цифры).

  • dwell    — время на вкладке: сколько секунд вкладка была открыта и видна
               (кабинет присылает, когда человек уходит с вкладки, сворачивает
               браузер или закрывает страницу). Больше 30 минут не учитывается —
               скорее всего, страницу просто забыли открытой.

Заходы из браузеров, где открывали CRM (тесты сотрудников под аккаунтами
клиентов), кабинет помечает флагом staff — такие события не сохраняются.

Семья — ученик и его родители, связанные в базе (пара кодов «Ученик +
родитель» при регистрации или привязка родителя в CRM), а не по фамилиям.
Доступ в кабинет считается выданным, если в CRM у ученика отмечено
«Ссылка выслана» (доступ в кабинет отправляют вместе с реферальной ссылкой).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.models import ChildProfile, LkEvent, ParentChild, ParentProfile, RoleEnum, User

router = APIRouter(tags=["lk-usage"])

KINDS = ("session", "payments", "schedule", "reports", "homework")
PAGES = {"parent": ("payments", "schedule", "reports"), "child": ("schedule", "homework")}
DWELL_PAGES = {"parent": ("schedule", "payments", "reports", "profile"), "child": ("schedule", "homework", "profile")}
DWELL_MAX = 1800
DWELL_EDGES = (0, 5, 10, 20, 40, 60, 120, 300)   # корзины гистограммы, секунды
SESSION_GAP = timedelta(minutes=30)
PAGE_GAP = timedelta(minutes=5)
CLIENT = "Клиент"


def _minsk(dt: datetime) -> datetime:
    return dt + timedelta(hours=3)


class TrackIn(BaseModel):
    kind: str
    platform: Optional[str] = None   # m — мобильный кабинет, d — сайт на компьютере
    staff: bool = False
    page: Optional[str] = None       # для kind="dwell"
    seconds: Optional[float] = None  # для kind="dwell"


@router.post("/lk/track")
async def lk_track(body: TrackIn, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if body.staff:
        return {"ok": True, "saved": False}
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role not in PAGES:
        return {"ok": True, "saved": False}
    if body.kind == "dwell":
        sec = int(round(body.seconds or 0))
        if body.page not in DWELL_PAGES[role] or sec < 1:
            return {"ok": True, "saved": False}
        now = datetime.utcnow()
        db.add(LkEvent(user_id=user.id, role=role, kind="dwell", page=body.page, seconds=min(sec, DWELL_MAX),
                       platform=body.platform if body.platform in ("m", "d") else None, at=now, day=_minsk(now).date()))
        await db.commit()
        return {"ok": True, "saved": ["dwell"]}
    if body.kind not in KINDS:
        return {"ok": True, "saved": False}
    if body.kind != "session" and body.kind not in PAGES[role]:
        return {"ok": True, "saved": False}
    now = datetime.utcnow()
    platform = body.platform if body.platform in ("m", "d") else None
    last_any = await db.scalar(select(func.max(LkEvent.at)).where(LkEvent.user_id == user.id))
    new_session = last_any is None or now - last_any > SESSION_GAP
    saved = []
    if new_session:
        db.add(LkEvent(user_id=user.id, role=role, kind="session", platform=platform, at=now, day=_minsk(now).date()))
        saved.append("session")
    if body.kind != "session":
        last_same = await db.scalar(select(func.max(LkEvent.at)).where(LkEvent.user_id == user.id, LkEvent.kind == body.kind))
        if last_same is None or now - last_same > PAGE_GAP:
            db.add(LkEvent(user_id=user.id, role=role, kind=body.kind, platform=platform, at=now, day=_minsk(now).date()))
            saved.append(body.kind)
    if saved:
        await db.commit()
    return {"ok": True, "saved": saved}


# ─── аналитика ────────────────────────────────────────────────────────────────

def _bucket_start(d: date, grain: str) -> date:
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    if grain == "year":
        return d.replace(month=1, day=1)
    return d


def _next_bucket(d: date, grain: str) -> date:
    if grain == "week":
        return d + timedelta(days=7)
    if grain == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    if grain == "year":
        return d.replace(year=d.year + 1, month=1, day=1)
    return d + timedelta(days=1)


MON = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _label(d: date, grain: str) -> str:
    if grain == "week":
        e = d + timedelta(days=6)
        return f"{d:%d.%m}–{e:%d.%m}"
    if grain == "month":
        return f"{MON[d.month - 1]} {d.year}"
    if grain == "year":
        return str(d.year)
    return f"{d:%d.%m}"


def _metrics(events: list, a: date, b: date) -> dict:
    """Сводные показатели за период [a, b] по списку событий."""
    ev = [e for e in events if a <= e["day"] <= b]
    sessions = [e for e in ev if e["kind"] == "session"]
    users = {e["user_id"] for e in ev}
    days = (b - a).days + 1
    by_day = defaultdict(set)
    for e in ev:
        by_day[e["day"]].add(e["user_id"])
    dau = sum(len(v) for v in by_day.values()) / days if days else 0
    views = {k: sum(1 for e in ev if e["kind"] == k) for k in KINDS if k != "session"}
    return {
        "sessions": len(sessions), "users": len(users), "dau": round(dau, 2),
        "sessions_per_user": round(len(sessions) / len(users), 2) if users else None,
        "pages_per_session": round(sum(views.values()) / len(sessions), 2) if sessions else None,
        "stickiness_pct": round(dau / len(users) * 100, 1) if users else None,
        "views": views,
        "mobile_pct": round(sum(1 for e in sessions if e["platform"] == "m") / len(sessions) * 100, 1) if sessions else None,
    }


@router.get("/analytics/lk", dependencies=[Depends(require_admin)])
async def lk_analytics(
    start: Optional[date] = Query(None), end: Optional[date] = Query(None),
    grain: str = Query("day"), db: AsyncSession = Depends(get_db),
):
    today = _minsk(datetime.utcnow()).date()
    first_day = await db.scalar(select(func.min(LkEvent.day)))
    end = min(end or today, today)
    start = start or first_day or today
    if start > end:
        start = end
    if grain not in ("day", "week", "month", "year"):
        grain = "day"
    if grain == "day" and (end - start).days > 400:
        grain = "week"
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)

    rows = (await db.execute(
        select(LkEvent.user_id, LkEvent.role, LkEvent.kind, LkEvent.platform, LkEvent.at, LkEvent.day, LkEvent.page, LkEvent.seconds)
        .where(LkEvent.day >= prev_start, LkEvent.day <= end)
    )).all()
    all_ev = [{"user_id": u, "role": r, "kind": k, "platform": p, "at": at, "day": d, "page": pg, "seconds": sc}
              for u, r, k, p, at, d, pg, sc in rows]
    dwell = [e for e in all_ev if e["kind"] == "dwell" and e["day"] >= start and e["seconds"]]
    events = [e for e in all_ev if e["kind"] != "dwell"]
    cur = [e for e in events if e["day"] >= start]
    prevs = [e for e in events if e["day"] < start]

    # ── ряды по периодам ──
    buckets = []
    b = _bucket_start(start, grain)
    while b <= end:
        buckets.append(b)
        b = _next_bucket(b, grain)
    series = {}
    for role in ("all", "parent", "child"):
        ev = cur if role == "all" else [e for e in cur if e["role"] == role]
        idx = defaultdict(list)
        for e in ev:
            idx[_bucket_start(e["day"], grain)].append(e)
        out = []
        for bs in buckets:
            be = min(_next_bucket(bs, grain) - timedelta(days=1), end)
            items = idx.get(bs, [])
            out.append({
                "start": bs.isoformat(), "end": be.isoformat(), "label": _label(bs, grain),
                "partial": bs < start or _next_bucket(bs, grain) - timedelta(days=1) > end,
                "sessions": sum(1 for e in items if e["kind"] == "session"),
                "users": len({e["user_id"] for e in items}),
                **{k: sum(1 for e in items if e["kind"] == k) for k in KINDS if k != "session"},
            })
        series[role] = out

    # ── профиль по дням недели и часам (сеансы) ──
    def profiles(ev):
        wd = [0] * 7
        hr = [0] * 24
        for e in ev:
            if e["kind"] != "session":
                continue
            m = _minsk(e["at"])
            wd[m.weekday()] += 1
            hr[m.hour] += 1
        n_wd = [0] * 7
        d = start
        while d <= end:
            n_wd[d.weekday()] += 1
            d += timedelta(days=1)
        return {"weekday_avg": [round(wd[i] / n_wd[i], 2) if n_wd[i] else 0 for i in range(7)], "hours": hr}
    prof = {role: profiles(cur if role == "all" else [e for e in cur if e["role"] == role]) for role in ("all", "parent", "child")}

    # ── люди: клиенты и все, кто заходил ──
    clients = (await db.execute(
        select(ChildProfile).options(
            joinedload(ChildProfile.user),
            joinedload(ChildProfile.parents).joinedload(ParentChild.parent).joinedload(ParentProfile.user),
        ).where(ChildProfile.crm_status == CLIENT)
    )).scalars().unique().all()
    people: dict[int, dict] = {}
    families = []

    def add_person(u: User, role: str, child: Optional[ChildProfile], status: str):
        p = people.get(u.id)
        if not p:
            p = people[u.id] = {"user_id": u.id, "role": role, "name": f"{u.last_name or ''} {u.first_name or ''}".strip() or u.email,
                                "registered": False, "children": [], "parents": [], "statuses": set(), "client": False}
        if child is not None and child.ref_sent_at:
            p["registered"] = True   # доступ выслан (чекбокс «Ссылка выслана» у ученика)
        p["statuses"].add(status or "")
        if status == CLIENT:
            p["client"] = True
        return p

    for ch in clients:
        if not ch.user:
            continue
        child_name = f"{ch.user.last_name or ''} {ch.user.first_name or ''}".strip()
        cp = add_person(ch.user, "child", ch, ch.crm_status)
        fam = {"child": ch.user.id, "parents": [], "access": bool(ch.ref_sent_at)}
        for link in ch.parents:
            pu = link.parent.user if link.parent else None
            if not pu:
                continue
            pp = add_person(pu, "parent", ch, ch.crm_status)
            if child_name not in pp["children"]:
                pp["children"].append(child_name)
            if pp["name"] not in cp["parents"]:
                cp["parents"].append(pp["name"])
            fam["parents"].append(pu.id)
        families.append(fam)

    # кто заходил в периоде, но не в списке клиентов (бывшие, пробные)
    extra_ids = {e["user_id"] for e in cur} - set(people)
    if extra_ids:
        extra = (await db.execute(
            select(User).options(
                joinedload(User.child_profile),
                joinedload(User.parent_profile).joinedload(ParentProfile.children).joinedload(ParentChild.child).joinedload(ChildProfile.user),
            ).where(User.id.in_(extra_ids))
        )).scalars().unique().all()
        for u in extra:
            role = u.role.value if hasattr(u.role, "value") else str(u.role)
            if role == "child" and u.child_profile:
                add_person(u, "child", u.child_profile, u.child_profile.crm_status)
            elif role == "parent" and u.parent_profile:
                p = None
                for link in u.parent_profile.children:
                    if link.child and link.child.user:
                        p = add_person(u, "parent", link.child, link.child.crm_status)
                        nm = f"{link.child.user.last_name or ''} {link.child.user.first_name or ''}".strip()
                        if nm not in p["children"]:
                            p["children"].append(nm)
                if p is None:
                    add_person(u, "parent", None, "")

    # всё время: первый и последний заход
    ids = list(people)
    alltime = {}
    if ids:
        for uid, fmin, fmax, n in (await db.execute(
            select(LkEvent.user_id, func.min(LkEvent.at), func.max(LkEvent.at), func.count(LkEvent.id))
            .where(LkEvent.user_id.in_(ids), LkEvent.kind == "session").group_by(LkEvent.user_id)
        )).all():
            alltime[uid] = (fmin, fmax, n)
    per = defaultdict(lambda: {"sessions": 0, "days": set(), "m": 0, "d": 0, **{k: 0 for k in KINDS if k != "session"}})
    for e in cur:
        s = per[e["user_id"]]
        s["days"].add(e["day"])
        if e["kind"] == "session":
            s["sessions"] += 1
            if e["platform"] in ("m", "d"):
                s[e["platform"]] += 1
        else:
            s[e["kind"]] += 1
    weeks = max(length / 7, 1)
    table = []
    for uid, p in people.items():
        s = per.get(uid)
        at = alltime.get(uid)
        if s and s["sessions"] + sum(s[k] for k in KINDS if k != "session"):
            state = "active"
        elif at:
            state = "idle"          # заходил раньше, но не в этом периоде
        elif p["registered"]:
            state = "never"         # доступ выслан, но ни разу не заходил
        else:
            state = "no_account"    # доступ в кабинет ещё не высылали
        table.append({
            "user_id": uid, "role": p["role"], "name": p["name"],
            "family": ", ".join(p["children"]) if p["role"] == "parent" else ", ".join(p["parents"]),
            "client": p["client"], "registered": p["registered"], "state": state,
            "sessions": s["sessions"] if s else 0, "days": len(s["days"]) if s else 0,
            "per_week": round((s["sessions"] if s else 0) / weeks, 2),
            "payments": s["payments"] if s else 0, "schedule": s["schedule"] if s else 0,
            "reports": s["reports"] if s else 0, "homework": s["homework"] if s else 0,
            "mobile": s["m"] if s else 0, "desktop": s["d"] if s else 0,
            "first_seen": _minsk(at[0]).isoformat() if at else None,
            "last_seen": _minsk(at[1]).isoformat() if at else None,
            "total_sessions": at[2] if at else 0,
        })

    # ── охват семей-клиентов ──
    active_ids = {e["user_id"] for e in cur}
    prev_active = {e["user_id"] for e in prevs}
    fam_total = len(families)
    fam_active = sum(1 for f in families if f["child"] in active_ids or any(p in active_ids for p in f["parents"]))
    fam_registered = sum(1 for f in families if f["access"])

    def role_summary(role):
        ev_c = cur if role == "all" else [e for e in cur if e["role"] == role]
        ev_p = prevs if role == "all" else [e for e in prevs if e["role"] == role]
        m = _metrics(ev_c, start, end)
        pm = _metrics(ev_p, prev_start, prev_end) if ev_p else None
        act = {e["user_id"] for e in ev_c}
        pact = {e["user_id"] for e in ev_p}
        clients_role = [p for p in people.values() if p["client"] and (role == "all" or p["role"] == role)]
        m["clients"] = len(clients_role)
        m["clients_registered"] = sum(1 for p in clients_role if p["registered"])
        m["clients_active"] = sum(1 for p in clients_role if p["user_id"] in act)
        m["coverage_pct"] = round(m["clients_active"] / m["clients"] * 100, 1) if m["clients"] else None
        m["retention_pct"] = round(len(act & pact) / len(pact) * 100, 1) if pact else None
        new_users = sum(1 for uid in act if alltime.get(uid) and start <= _minsk(alltime[uid][0]).date() <= end)
        m["new_users"] = new_users
        return {"current": m, "previous": pm}

    # ── время на вкладках ──
    def q(vals, f):
        if not vals:
            return None
        v = sorted(vals)
        k = (len(v) - 1) * f
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)

    def dwell_stats(role):
        out = {}
        pages = sorted(set(DWELL_PAGES["parent"]) | set(DWELL_PAGES["child"])) if role == "all" else DWELL_PAGES[role]
        for page in pages:
            items = [e for e in dwell if e["page"] == page and (role == "all" or e["role"] == role)]
            secs = [e["seconds"] for e in items]
            hist = [0] * len(DWELL_EDGES)
            for s in secs:
                i = len(DWELL_EDGES) - 1
                while i > 0 and s < DWELL_EDGES[i]:
                    i -= 1
                hist[i] += 1
            idx = defaultdict(list)
            for e in items:
                idx[_bucket_start(e["day"], grain)].append(e["seconds"])
            out[page] = {
                "n": len(secs), "avg": round(sum(secs) / len(secs), 1) if secs else None,
                "median": q(secs, .5), "p25": q(secs, .25), "p75": q(secs, .75),
                "under5_pct": round(sum(1 for s in secs if s < 5) / len(secs) * 100, 1) if secs else None,
                "hist": hist,
                "series": [{"label": _label(bs, grain), "start": bs.isoformat(), "n": len(idx.get(bs, [])),
                            "median": q(idx.get(bs, []), .5),
                            "avg": round(sum(idx[bs]) / len(idx[bs]), 1) if idx.get(bs) else None} for bs in buckets],
            }
        return out

    return {
        "dwell": {role: dwell_stats(role) for role in ("all", "parent", "child")},
        "dwell_edges": list(DWELL_EDGES),
        "start": start.isoformat(), "end": end.isoformat(), "grain": grain, "today": today.isoformat(),
        "prev_start": prev_start.isoformat(), "prev_end": prev_end.isoformat(),
        "first_day": first_day.isoformat() if first_day else None,
        "summary": {role: role_summary(role) for role in ("all", "parent", "child")},
        "families": {"total": fam_total, "registered": fam_registered, "active": fam_active,
                     "active_pct": round(fam_active / fam_total * 100, 1) if fam_total else None},
        "series": series, "profiles": prof, "table": table,
    }


# ─── выгрузка для похожей аудитории (Lookalike) в Meta ───────────────────────
# Формат шаблона Meta: CSV с колонками email и phone. Телефон — только цифры,
# белорусский номер строго 375XXXXXXXXX (12 цифр); email — строчными буквами.
# Берём контакты родителей (они платят и принимают решение): телефон и email
# из карточки родителя и из договора. Данные учеников не выгружаем — это
# несовершеннолетние, Meta запрещает загружать их данные для рекламы.

import csv
import io
import re

from fastapi import Response

from app.models.models import CrmStatusEvent, ParentContract, ParentContractChild

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}$")


def _norm_phone_meta(p: Optional[str]) -> Optional[str]:
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    if not d:
        return None
    if len(d) == 11 and d.startswith("80"):
        d = "375" + d[2:]
    elif len(d) == 9:
        d = "375" + d
    if d.startswith("375"):
        return d if len(d) == 12 and set(d[3:]) != {"0"} else None
    return d if 10 <= len(d) <= 15 else None   # иностранный номер с кодом страны


def _norm_email_meta(e: Optional[str]) -> Optional[str]:
    e = (e or "").strip().lower()
    if not e or e.endswith("@pifagor.local") or not _EMAIL_RE.match(e):
        return None
    return e


@router.get("/analytics/lookalike", dependencies=[Depends(require_admin)])
async def lookalike_csv(scope: str = Query("current"), kids: bool = Query(False), db: AsyncSession = Depends(get_db)):
    """scope=current — нынешние клиенты; scope=ever — все, кто когда-либо был клиентом.
    kids=1 — если у семьи нет ни телефона, ни email родителя, взять номер из карточки
    ученика (в CRM для младших учеников там часто записан номер родителя)."""
    if scope == "ever":
        ids = set((await db.execute(select(ChildProfile.id).where(ChildProfile.crm_status == CLIENT))).scalars().all())
        ids |= set((await db.execute(select(CrmStatusEvent.child_id).where(CrmStatusEvent.new_status == CLIENT).distinct())).scalars().all())
    else:
        ids = set((await db.execute(select(ChildProfile.id).where(ChildProfile.crm_status == CLIENT))).scalars().all())
    pairs: list[tuple[Optional[str], Optional[str]]] = []
    if ids:
        for email, phone in (await db.execute(
            select(User.email, User.phone)
            .join(ParentProfile, ParentProfile.user_id == User.id)
            .join(ParentChild, ParentChild.parent_id == ParentProfile.id)
            .where(ParentChild.child_id.in_(ids))
        )).all():
            pairs.append((_norm_email_meta(email), _norm_phone_meta(phone)))
        contract_ids = set((await db.execute(select(ParentContract.id).where(ParentContract.child_id.in_(ids)))).scalars().all())
        contract_ids |= set((await db.execute(select(ParentContractChild.contract_id).where(ParentContractChild.child_id.in_(ids)))).scalars().all())
        if contract_ids:
            for email, phone in (await db.execute(
                select(ParentContract.parent_email, ParentContract.parent_phone).where(ParentContract.id.in_(contract_ids))
            )).all():
                pairs.append((_norm_email_meta(email), _norm_phone_meta(phone)))
    if kids and ids:
        have = set()
        for cid, email, phone in (await db.execute(
            select(ParentChild.child_id, User.email, User.phone)
            .join(ParentProfile, ParentProfile.id == ParentChild.parent_id).join(User, User.id == ParentProfile.user_id)
            .where(ParentChild.child_id.in_(ids))
        )).all():
            if _norm_email_meta(email) or _norm_phone_meta(phone):
                have.add(cid)
        for cid, email, phone in (await db.execute(
            select(ParentContract.child_id, ParentContract.parent_email, ParentContract.parent_phone).where(ParentContract.child_id.in_(ids))
        )).all():
            if _norm_email_meta(email) or _norm_phone_meta(phone):
                have.add(cid)
        for cid, phone in (await db.execute(
            select(ChildProfile.id, User.phone).join(User, User.id == ChildProfile.user_id).where(ChildProfile.id.in_(ids - have))
        )).all():
            pairs.append((None, _norm_phone_meta(phone)))
    # убираем пустые и дубли; строку «только телефон» или «только email» не пишем,
    # если этот же телефон / email уже есть в строке вместе с парой
    full = {(e, p) for e, p in pairs if e and p}
    full_phones = {p for _, p in full}
    full_emails = {e for e, _ in full}
    rows = sorted(full)
    rows += sorted({("", p) for e, p in pairs if p and not e and p not in full_phones})
    rows += sorted({(e, "") for e, p in pairs if e and not p and e not in full_emails})
    rows.sort(key=lambda r: (r[0] == "", r[0], r[1]))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["email", "phone"])
    w.writerows(rows)
    name = f"lookalike-pifagor-{'all' if scope == 'ever' else 'clients'}-{_minsk(datetime.utcnow()).date().isoformat()}.csv"
    return Response(content=buf.getvalue().encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
                             "X-Rows": str(len(rows)), "Access-Control-Expose-Headers": "X-Rows"})
