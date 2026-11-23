"""Controls for running the workspace as a public, shared demo.

The local server assumes one trusted user. A public URL breaks that assumption, so
this module adds what a shared demo needs: per-visitor ownership of conversations
and runs, per-client request limits, a global daily spend ceiling, a concurrency
cap, and read-only access to bundled evaluation evidence. Limits are estimates
checked between requests; set a hard spend limit on the provider account as well.
"""
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path

SESSION_COOKIE = "ar_session"
_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
# Used when a run has no cost estimate, so missing pricing can never mean "free".
CONSERVATIVE_RUN_COST_USD = 0.03


class DemoLimit(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _env_number(name, default, cast=float, minimum=0):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = cast(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


class PublicSettings:
    def __init__(self, hostnames, per_minute=5, per_day=30, global_per_day=400, daily_budget_usd=1.0,
                 max_concurrent=3, max_message_chars=1000, ip_header="True-Client-IP",
                 reports_dir=None, runs_dir=None):
        if not hostnames:
            raise ValueError("Public mode needs at least one hostname (PUBLIC_HOSTNAMES or RENDER_EXTERNAL_HOSTNAME)")
        self.hostnames = sorted({name.strip().lower() for name in hostnames if name.strip()})
        self.per_minute = per_minute
        self.per_day = per_day
        self.global_per_day = global_per_day
        self.daily_budget_usd = daily_budget_usd
        self.max_concurrent = max_concurrent
        self.max_message_chars = max_message_chars
        self.ip_header = ip_header
        self.reports_dir = Path(reports_dir) if reports_dir else None
        self.runs_dir = Path(runs_dir) if runs_dir else None

    @classmethod
    def from_environment(cls, root):
        names = [name for name in os.environ.get("PUBLIC_HOSTNAMES", "").split(",") if name.strip()]
        if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
            names.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
        return cls(names,
                   per_minute=_env_number("DEMO_PER_MINUTE", 5, int, 1),
                   per_day=_env_number("DEMO_PER_DAY", 30, int, 1),
                   global_per_day=_env_number("DEMO_GLOBAL_PER_DAY", 400, int, 1),
                   daily_budget_usd=_env_number("DEMO_DAILY_BUDGET_USD", 1.0),
                   max_concurrent=_env_number("DEMO_MAX_CONCURRENT", 3, int, 1),
                   max_message_chars=_env_number("DEMO_MAX_MESSAGE_CHARS", 1000, int, 1),
                   ip_header=os.environ.get("DEMO_CLIENT_IP_HEADER", "True-Client-IP"),
                   reports_dir=Path(root) / "demo" / "reports",
                   runs_dir=Path(root) / "demo" / "runs")


class DemoGuard:
    """Request limits and spend accounting. All counters reset at UTC midnight."""

    def __init__(self, settings, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.lock = threading.Lock()
        self.minute = {}
        self.day_counts = {}
        self.day = None
        self.global_requests = 0
        self.spent_usd = 0.0
        self.active = 0

    def _roll(self):
        today = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        if today != self.day:
            self.day, self.day_counts, self.global_requests, self.spent_usd = today, {}, 0, 0.0
            self.minute = {}

    def client_ip(self, headers, socket_ip):
        value = headers.get(self.settings.ip_header) if self.settings.ip_header else None
        if value and value.strip():
            return value.strip()[:64]
        forwarded = headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        return socket_ip

    def admit(self, ip):
        """Reserve one model request for this client or raise DemoLimit."""
        with self.lock:
            self._roll()
            now = self.clock()
            if self.spent_usd >= self.settings.daily_budget_usd:
                raise DemoLimit(429, "The demo has reached today's spending limit. Please try again tomorrow, "
                                     "or browse the saved answers under Quality checks.")
            if self.global_requests >= self.settings.global_per_day:
                raise DemoLimit(429, "The demo has reached today's question limit. Please try again tomorrow.")
            if self.day_counts.get(ip, 0) >= self.settings.per_day:
                raise DemoLimit(429, f"You've asked {self.settings.per_day} questions today, the demo's daily limit. "
                                     "Please come back tomorrow.")
            window = self.minute.setdefault(ip, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.settings.per_minute:
                raise DemoLimit(429, "That's a lot of questions in a minute. Please wait a moment and try again.")
            if self.active >= self.settings.max_concurrent:
                raise DemoLimit(503, "The demo is busy answering other visitors. Please try again in a few seconds.")
            window.append(now)
            self.day_counts[ip] = self.day_counts.get(ip, 0) + 1
            self.global_requests += 1
            self.active += 1

    def release(self, cost_usd):
        with self.lock:
            self.active = max(0, self.active - 1)
            self._roll()
            cost = cost_usd if isinstance(cost_usd, (int, float)) and cost_usd >= 0 else CONSERVATIVE_RUN_COST_USD
            self.spent_usd += cost

    def light_check(self, ip, limit=20):
        """Free operations (creating a conversation) share a looser per-minute window."""
        with self.lock:
            self._roll()
            now = self.clock()
            window = self.minute.setdefault("free:" + ip, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= limit:
                raise DemoLimit(429, "Too many requests. Please wait a moment.")
            window.append(now)

    def status(self, ip):
        with self.lock:
            self._roll()
            return {"questions_per_day": self.settings.per_day,
                    "questions_left_today": max(0, self.settings.per_day - self.day_counts.get(ip, 0)),
                    "questions_per_minute": self.settings.per_minute,
                    "daily_budget_reached": self.spent_usd >= self.settings.daily_budget_usd,
                    "max_message_chars": self.settings.max_message_chars}


class Ownership:
    """Which conversations and runs belong to which browser session."""

    def __init__(self, max_sessions=20000):
        self.sessions = OrderedDict()
        self.max_sessions = max_sessions
        self.lock = threading.Lock()

    @staticmethod
    def valid(session):
        return isinstance(session, str) and bool(_ID_PATTERN.fullmatch(session))

    @staticmethod
    def new_session():
        return secrets.token_hex(16)

    def _entry(self, session):
        entry = self.sessions.get(session)
        if entry is None:
            entry = {"conversations": set(), "runs": set()}
            self.sessions[session] = entry
            while len(self.sessions) > self.max_sessions:
                self.sessions.popitem(last=False)
        else:
            self.sessions.move_to_end(session)
        return entry

    def add(self, session, kind, item_id):
        with self.lock:
            self._entry(session)[kind].add(item_id)

    def items(self, session, kind):
        with self.lock:
            entry = self.sessions.get(session)
            return list(entry[kind]) if entry else []

    def owns(self, session, kind, item_id):
        with self.lock:
            entry = self.sessions.get(session)
            return bool(entry) and item_id in entry[kind]


def bundled_run_ids(runs_dir):
    if not runs_dir or not Path(runs_dir).is_dir():
        return set()
    return {path.stem for path in Path(runs_dir).glob("*.json") if _ID_PATTERN.fullmatch(path.stem)}


def load_bundled_run(runs_dir, run_id):
    if not _ID_PATTERN.fullmatch(run_id):
        raise ValueError("Invalid run ID")
    return json.loads((Path(runs_dir) / (run_id + ".json")).read_text(encoding="utf-8"))


def bundled_reports(reports_dir, limit=20):
    if not reports_dir or not Path(reports_dir).is_dir():
        return []
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in Path(reports_dir).glob("*.json")
               if _ID_PATTERN.fullmatch(path.stem)]
    reports.sort(key=lambda report: report.get("created_at", ""), reverse=True)
    return reports[:limit]


def parse_cookie(header):
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value
    return None
