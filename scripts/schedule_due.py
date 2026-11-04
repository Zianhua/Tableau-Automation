"""R1 / F-01 (D-036): which Singapore dates does a Tableau subscription's schedule fire on, and is a subscription DUE today?

Pure functions first (unit-tested in scripts/r1_checks.py), then the small DWH loaders used by t4_refresh.py and t6_send.py.

THE RULE (D-036). A subscription has an OCCURRENCE on every Singapore date its Tableau schedule would have fired
(daily/hourly: every date, or only the listed weekdays; weekly: the listed weekdays; monthly: day number, last day,
or 'first Monday' style). Time of day is ignored; "today" is the Singapore date (D-016). For an occurrence O:
  - it can be satisfied on O itself or on any of the next CATCH_UP days (default 3), but never after the NEXT occurrence
    starts: window(O) = [O, min(O + catch_up, next occurrence - 1 day)]. A daily schedule therefore has a one-day window;
    a monthly one has O..O+3, so "1st of the month" on a Saturday is sent on the first following day on which the data is
    ready (Monday the 3rd if no ETL ran at the weekend). Day 1 is never moved: the calendar date stays the occurrence.
  - a subscription is DUE today when today is inside the window of an occurrence that no send satisfied. The send log
    is the memory: a row with status Sent, Claimed or TimedOut (all three keep the slot) whose SendDate is inside the
    window satisfies it. Hence the same occurrence is never sent twice, and a catch-up day that already sent does not
    send again. Released/Failed/DryRun do not satisfy it.
  - the window ending without a send is reported once (missed, on the day after the window end).
  - occurrences before the script took the subscription over (Tableau_SubscriptionBackup.PausedAt) belong to the old
    Tableau subscription and are ignored; an occurrence on the takeover day counts only if its start time is later
    than the pause time.
  - a schedule that is unreadable, unsupported, or not synced recently is NOT due and is reported (fail closed). The schedule's
    enabled / disabled state is not looked at (D-080): only its calendar counts, for subscriptions and extract refresh tasks alike.
Assumptions from recall, to verify on prod: Tableau schedules run in the server's time zone = Singapore; a monthly day
beyond the end of a short month (31 in April) fires on the last day of that month.
"""
import calendar
import datetime as dt
import os
from dataclasses import dataclass, field

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ORDINALS = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Fifth": 5, "Last": -1}
DEFAULT_CATCH_UP_DAYS = 3
SGT = dt.timezone(dt.timedelta(hours=8))
MAX_LOOKAHEAD_DAYS = 400


def catch_up_default(env=os.environ):
    try:
        return max(0, int(env.get("SCHEDULE_CATCHUP_DAYS", DEFAULT_CATCH_UP_DAYS)))
    except ValueError:
        return DEFAULT_CATCH_UP_DAYS


@dataclass(frozen=True)
class Schedule:
    id: str
    name: str = ""
    state: str = "Active"
    frequency: str = ""                 # Hourly | Daily | Weekly | Monthly
    weekdays: frozenset = frozenset()   # 0 = Monday
    month_tokens: tuple = ()            # '1'..'31', 'LastDay', ordinals and weekday names as Tableau returns them
    start: object = None                # dt.time
    interval_hours: object = None       # float, Hourly/Daily only
    last_seen: object = None            # naive Singapore datetime of the last T2 sync that read it


# ---------- reading a TSC ScheduleItem (T2) ----------
def from_tsc(item):
    """Schedule from a tableauserverclient ScheduleItem. Raises ValueError for a shape that is not understood."""
    iv = item.interval_item
    freq = getattr(iv, "_frequency", None) or type(iv).__name__.replace("Interval", "")
    vals = list(getattr(iv, "interval", ()) or ())
    weekdays, tokens, hours = set(), [], None
    for v in vals:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit()):
            if freq == "Monthly":
                tokens.append(str(int(float(v))))
            else:
                hours = float(v)
        elif v in DAY_NAMES and freq != "Monthly":
            weekdays.add(DAY_NAMES.index(v))
        else:
            tokens.append(str(v))
    return Schedule(id=item.id, name=item.name or "", state=item.state or "", frequency=freq, weekdays=frozenset(weekdays),
                    month_tokens=tuple(tokens), start=getattr(iv, "start_time", None), interval_hours=hours)


def to_row(s):
    """DWH row values for Integration.Tableau_Schedule (id, name, state, frequency, weekdays, month tokens, start, hours)."""
    return (s.id, s.name[:200], s.state[:12], s.frequency[:10], ",".join(DAY_NAMES[d][:3] for d in sorted(s.weekdays)) or None,
            ",".join(s.month_tokens) or None, s.start.replace(microsecond=0).isoformat() if s.start else None, s.interval_hours)


def from_row(r):
    wd = {[n[:3] for n in DAY_NAMES].index(x) for x in (r[4] or "").split(",") if x}
    start = dt.time.fromisoformat(str(r[6])[:8]) if r[6] is not None else None
    return Schedule(id=r[0], name=r[1] or "", state=r[2] or "", frequency=r[3] or "", weekdays=frozenset(wd),
                    month_tokens=tuple(x for x in (r[5] or "").split(",") if x), start=start,
                    interval_hours=None if r[7] is None else float(r[7]), last_seen=r[8])


# ---------- the calendar (pure) ----------
class Unsupported(Exception):
    """The schedule shape is not understood: the subscription is treated as not due and reported."""


def _monthly_fires(s, d):
    toks = list(s.month_tokens)
    if not toks:
        raise Unsupported("monthly schedule without a day")
    if "Customized Monthly" in toks:
        raise Unsupported("customized monthly schedule")
    nums = [t for t in toks if t.isdigit()]
    ords = [t for t in toks if t in ORDINALS]
    wds = [t for t in toks if t in DAY_NAMES]
    last = "LastDay" in toks
    if len(nums) + len(ords) + len(wds) + int(last) != len(toks):
        raise Unsupported("unknown monthly token in %s" % (toks,))
    last_dom = calendar.monthrange(d.year, d.month)[1]
    if ords or wds:
        if nums or last or not ords or not wds:
            raise Unsupported("mixed monthly tokens %s" % (toks,))
        for o in ords:
            n = ORDINALS[o]
            for w in wds:
                if d.weekday() != DAY_NAMES.index(w):
                    continue
                if (n > 0 and (d.day - 1) // 7 + 1 == n) or (n < 0 and d.day + 7 > last_dom):
                    return True
        return False
    return (last and d.day == last_dom) or any(min(int(n), last_dom) == d.day for n in nums)


def fires_on(s, d):
    """True/False: does the schedule fire on date d. Raises Unsupported. State is NOT looked at here."""
    f = s.frequency
    if f in ("Hourly", "Daily"):
        if s.interval_hours is not None and s.interval_hours > 24:
            raise Unsupported("interval longer than a day (%s h)" % s.interval_hours)
        return not s.weekdays or d.weekday() in s.weekdays
    if f == "Weekly":
        if not s.weekdays:
            raise Unsupported("weekly schedule without weekdays")
        return d.weekday() in s.weekdays
    if f == "Monthly":
        return _monthly_fires(s, d)
    raise Unsupported("frequency %r" % f)


def next_occurrence(s, after):
    d = after
    for _ in range(MAX_LOOKAHEAD_DAYS):
        d += dt.timedelta(days=1)
        if fires_on(s, d):
            return d
    return None


def window_end(s, occ, catch_up):
    end = occ + dt.timedelta(days=catch_up)
    nxt = next_occurrence(s, occ)
    if nxt is not None:
        end = min(end, nxt - dt.timedelta(days=1))
    return end


@dataclass
class DueResult:
    status: str                  # due | satisfied | not_due | unsupported
    occurrence: object = None    # the occurrence date being worked on (due / satisfied)
    missed: object = None        # occurrence whose window ended yesterday without a send
    taken_today: bool = False    # an occurrence in its window was ignored because the takeover pause came after it
    window_end: object = None
    note: str = ""


def _counts(s, occ, not_before):
    """An occurrence before the takeover time belongs to the old Tableau subscription."""
    if not_before is None:
        return True
    at = dt.datetime.combine(occ, s.start) if s.start else dt.datetime.combine(occ, dt.time.max)
    return at > not_before


def evaluate(s, today, sent_dates, catch_up=DEFAULT_CATCH_UP_DAYS, not_before=None):
    """DueResult for schedule `s` on Singapore date `today`. sent_dates: SendDates of Sent/Claimed/TimedOut log rows.
    not_before: naive Singapore datetime of the takeover (None = no restriction)."""
    sent = set(sent_dates)
    try:
        lo = today - dt.timedelta(days=catch_up + 1)
        occs = []
        d = lo
        ignored = []
        while d <= today:
            if fires_on(s, d):
                if _counts(s, d, not_before):
                    occs.append(d)
                else:
                    ignored.append(d)
            d += dt.timedelta(days=1)
        res = DueResult("not_due")
        res.taken_today = any(o <= today <= window_end(s, o, catch_up) and o == not_before.date() for o in ignored) if not_before else False
        for occ in occs:
            end = window_end(s, occ, catch_up)
            satisfied = any(occ <= x <= end for x in sent)
            if end == today - dt.timedelta(days=1) and not satisfied:
                res.missed = occ
            if occ <= today <= end:
                res.status, res.occurrence, res.window_end = ("satisfied" if satisfied else "due"), occ, end
    except Unsupported as e:
        return DueResult("unsupported", note=str(e))
    return res


# ---------- DWH loaders ----------
SCHEDULE_SQL = """SELECT Schedule_id, Name, State, Frequency, WeekDays, MonthDays, CAST(StartTime AS varchar(8)), IntervalHours, LastSeenAt
FROM Integration.Tableau_Schedule WHERE Active_Flag = 1"""
HISTORY_SQL = """SELECT Subscription_id, SendDate FROM Integration.Tableau_SubscriptionSendLog
WHERE Status IN ('Sent', 'Claimed', 'TimedOut') AND SendDate >= ?"""


def load_schedules(cur):
    return {r[0]: from_row(r) for r in cur.execute(SCHEDULE_SQL).fetchall()}


def load_history(cur, today, catch_up):
    out = {}
    for sid, d in cur.execute(HISTORY_SQL, (today - dt.timedelta(days=catch_up + 3)).isoformat()).fetchall():
        out.setdefault(sid, set()).add(d)
    return out


@dataclass
class DueEngine:
    """Per run: schedules and send history loaded once. status() never raises."""
    today: dt.date
    schedules: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    catch_up: int = DEFAULT_CATCH_UP_DAYS
    max_age_h: float = 26
    now: object = None      # naive Singapore datetime, injectable for tests

    @classmethod
    def load(cls, cur, today, catch_up=None, max_age_h=26):
        c = catch_up_default() if catch_up is None else catch_up
        return cls(today, load_schedules(cur), load_history(cur, today, c), c, max_age_h,
                   dt.datetime.now(SGT).replace(tzinfo=None))

    def status(self, sub_id, schedule_id, taken_at=None):
        s = self.schedules.get(schedule_id) if schedule_id else None
        if s is None:
            return DueResult("unsupported", note="no schedule stored for this subscription (run t2_mapping_sync.py)")
        if s.last_seen is not None and self.now is not None:
            age = (self.now - s.last_seen).total_seconds() / 3600
            if age > self.max_age_h:
                return DueResult("unsupported", note="schedule last synced %.1f h ago (limit %.0f h)" % (age, self.max_age_h))
        return evaluate(s, self.today, self.history.get(sub_id, ()), self.catch_up, taken_at)
