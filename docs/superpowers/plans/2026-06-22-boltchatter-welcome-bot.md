# BoltChatter Welcome Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A headless Python service (BoltChatter.com, on Railway) that welcomes new Fanvue subscribers ~60s after Fanvue's own auto-welcome, pings the operator on Telegram for each new sub, and alerts on Telegram when any chat goes unanswered for ≥5 minutes — working in both agency and single-creator mode.

**Architecture:** A single asyncio process runs four cooperating loops (poller, anchor, scheduler, watchdog) over durable SQLite state. All Fanvue HTTP goes through one rate-limited async client behind a `SubscriberSource` interface with two implementations (`AgencySource`, `CreatorSource`) selected by `FANVUE_MODE`. Every loop has a pure `run_once(...)` that is unit-tested against a `FakeSource` + `FakeClock` + in-memory SQLite, so no test ever touches the network.

**Tech Stack:** Python 3.11+, `asyncio`, `httpx` (async HTTP + `MockTransport` for tests), stdlib `sqlite3`, `pytest` + `pytest-asyncio`. Deployed as a Railway worker with a persistent volume at `/data`.

**Spec:** `docs/superpowers/specs/2026-06-22-fanvue-welcome-bot-design.md` — read it before starting.

**Conventions used in this plan:**
- All package code lives under `boltchatter/`. All tests under `tests/`.
- Timestamps are stored as ISO-8601 UTC strings (`datetime.isoformat()`), always timezone-aware.
- `SELF = "self"` is the sentinel creator UUID used in single-creator mode.
- Job statuses: `AWAITING_GENERIC`, `PENDING`, `SENT`, `FAILED`, `EXPIRED`.

---

## File Structure

```
boltchatter/
  __init__.py
  config.py            # Config dataclass loaded from env
  clock.py             # Clock protocol + RealClock + FakeClock
  models.py            # NewSub, UnansweredChat dataclasses; status constants
  store.py             # SQLite state: seen, welcome_jobs, unanswered_watch, meta
  telegram.py          # best-effort async Telegram sender + alert formatters
  fanvue/
    __init__.py
    client.py          # FanvueClient: async httpx wrapper, auth headers, 429 handling
    source.py          # SubscriberSource protocol + SELF sentinel
    creator_source.py  # CreatorSource (single-creator token)
    agency_source.py   # AgencySource (agency token, many creators)
  loops/
    __init__.py
    poller.py          # new-sub detection + cold-start bootstrap
    anchor.py          # AWAITING_GENERIC -> PENDING once generic welcome seen
    scheduler.py       # send pending welcomes
    watchdog.py        # unanswered-chat alerts
  health.py            # tiny /health server for Railway
  app.py               # wires config/store/source/loops; main()
tests/
  conftest.py          # shared fixtures: in-memory store, FakeClock, FakeSource
  test_config.py
  test_clock.py
  test_store.py
  test_telegram.py
  test_fanvue_client.py
  test_sources.py
  test_poller.py
  test_anchor.py
  test_scheduler.py
  test_watchdog.py
pyproject.toml
.env.example
Dockerfile
README.md
```

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `boltchatter/__init__.py`
- Create: `boltchatter/fanvue/__init__.py`
- Create: `boltchatter/loops/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

`tests/test_smoke.py`:
```python
def test_package_imports():
    import boltchatter
    assert boltchatter.__name__ == "boltchatter"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'boltchatter'`

- [ ] **Step 3: Create the package + config files**

`pyproject.toml`:
```toml
[project]
name = "boltchatter"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Create empty files: `boltchatter/__init__.py`, `boltchatter/fanvue/__init__.py`, `boltchatter/loops/__init__.py`, `tests/__init__.py`.

- [ ] **Step 4: Install and run the test**

Run: `python -m pip install -e ".[dev]" && python -m pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml boltchatter tests
git commit -m "chore: scaffold boltchatter package"
```

---

## Task 2: Clock

**Files:**
- Create: `boltchatter/clock.py`
- Test: `tests/test_clock.py`

- [ ] **Step 1: Write the failing test**

`tests/test_clock.py`:
```python
from datetime import datetime, timezone, timedelta
from boltchatter.clock import RealClock, FakeClock


def test_real_clock_is_utc_aware():
    now = RealClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fake_clock_advances():
    start = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    clock = FakeClock(start)
    assert clock.now() == start
    clock.advance(seconds=90)
    assert clock.now() == start + timedelta(seconds=90)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_clock.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'boltchatter.clock'`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/clock.py`:
```python
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, start: datetime) -> None:
        assert start.tzinfo is not None, "FakeClock requires a tz-aware datetime"
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float = 0, minutes: float = 0) -> None:
        self._now = self._now + timedelta(seconds=seconds, minutes=minutes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_clock.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/clock.py tests/test_clock.py
git commit -m "feat: add injectable Clock with FakeClock for tests"
```

---

## Task 3: Models

**Files:**
- Create: `boltchatter/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
from boltchatter.models import NewSub, UnansweredChat, Status


def test_newsub_fields():
    s = NewSub(creator_uuid="c1", sub_uuid="u1", display_name="Jess", handle="jess")
    assert s.creator_uuid == "c1" and s.handle == "jess"


def test_unanswered_fields():
    c = UnansweredChat(
        creator_uuid="c1", user_uuid="u1", handle="jess", display_name="Jess",
        last_message_uuid="m1", last_message_text="hey there",
    )
    assert c.last_message_uuid == "m1"


def test_status_constants():
    assert Status.AWAITING_GENERIC == "AWAITING_GENERIC"
    assert Status.PENDING == "PENDING"
    assert Status.SENT == "SENT"
    assert Status.FAILED == "FAILED"
    assert Status.EXPIRED == "EXPIRED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/models.py`:
```python
from __future__ import annotations

from dataclasses import dataclass


class Status:
    AWAITING_GENERIC = "AWAITING_GENERIC"
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class NewSub:
    creator_uuid: str
    sub_uuid: str
    display_name: str
    handle: str


@dataclass(frozen=True)
class UnansweredChat:
    creator_uuid: str
    user_uuid: str
    handle: str
    display_name: str
    last_message_uuid: str
    last_message_text: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/models.py tests/test_models.py
git commit -m "feat: add NewSub/UnansweredChat models and Status constants"
```

---

## Task 4: Config

**Files:**
- Create: `boltchatter/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
import pytest
from boltchatter.config import Config


def base_env():
    return {
        "FANVUE_API_TOKEN": "tok",
        "FANVUE_MODE": "creator",
        "TELEGRAM_BOT_TOKEN": "btok",
        "TELEGRAM_CHAT_ID": "123",
    }


def test_defaults_applied():
    cfg = Config.from_env(base_env())
    assert cfg.fanvue_token == "tok"
    assert cfg.mode == "creator"
    assert cfg.api_version == "2025-06-26"
    assert cfg.welcome_delay_seconds == 60
    assert cfg.welcome_folder == "Welcome"
    assert cfg.poll_interval_seconds == 30
    assert cfg.anchor_timeout_minutes == 30
    assert cfg.unanswered_threshold_minutes == 5
    assert cfg.unanswered_renotify_minutes == 0
    assert cfg.watchdog_enabled is True
    assert cfg.dry_run is False
    assert cfg.db_path == "/data/boltchatter.db"


def test_missing_required_raises():
    env = base_env()
    del env["FANVUE_API_TOKEN"]
    with pytest.raises(ValueError, match="FANVUE_API_TOKEN"):
        Config.from_env(env)


def test_invalid_mode_raises():
    env = base_env()
    env["FANVUE_MODE"] = "nonsense"
    with pytest.raises(ValueError, match="FANVUE_MODE"):
        Config.from_env(env)


def test_per_creator_overrides_and_bools():
    env = base_env()
    env["FANVUE_MODE"] = "agency"
    env["WATCHDOG_ENABLED"] = "false"
    env["DRY_RUN"] = "true"
    env["WELCOME_TEXT_abc-123"] = "hi {name} from abc"
    cfg = Config.from_env(env)
    assert cfg.watchdog_enabled is False
    assert cfg.dry_run is True
    assert cfg.welcome_text_overrides["abc-123"] == "hi {name} from abc"


def test_welcome_text_for_falls_back_to_default():
    cfg = Config.from_env(base_env())
    assert "{name}" in cfg.welcome_text_for("any-creator") or cfg.welcome_text_for("any") != ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/config.py`:
```python
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

DEFAULT_WELCOME = (
    "Love, so happy you actually came! \U0001F970 What's your TG name btw? "
    "I feel way safer sharing pics and videos here and we can really get to "
    "know each other \U0001F608"
)


def _bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    fanvue_token: str
    mode: str  # "agency" | "creator"
    telegram_bot_token: str
    telegram_chat_id: str

    api_version: str = "2025-06-26"
    welcome_delay_seconds: int = 60
    welcome_folder: str = "Welcome"
    welcome_text: str = DEFAULT_WELCOME
    welcome_text_overrides: Mapping[str, str] = field(default_factory=dict)

    poll_interval_seconds: int = 30
    anchor_interval_seconds: int = 20
    anchor_timeout_minutes: int = 30

    unanswered_threshold_minutes: int = 5
    unanswered_poll_interval_seconds: int = 60
    unanswered_renotify_minutes: int = 0
    unanswered_subscribers_only: bool = False
    watchdog_enabled: bool = True

    db_path: str = "/data/boltchatter.db"
    dry_run: bool = False
    health_port: int = 8080

    def welcome_text_for(self, creator_uuid: str) -> str:
        return self.welcome_text_overrides.get(creator_uuid, self.welcome_text)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)

        def req(key: str) -> str:
            v = env.get(key)
            if not v:
                raise ValueError(f"Missing required env var: {key}")
            return v

        mode = req("FANVUE_MODE")
        if mode not in ("agency", "creator"):
            raise ValueError(f"FANVUE_MODE must be 'agency' or 'creator', got {mode!r}")

        overrides = {
            k[len("WELCOME_TEXT_"):]: v
            for k, v in env.items()
            if k.startswith("WELCOME_TEXT_")
        }

        return cls(
            fanvue_token=req("FANVUE_API_TOKEN"),
            mode=mode,
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=req("TELEGRAM_CHAT_ID"),
            api_version=env.get("FANVUE_API_VERSION", "2025-06-26"),
            welcome_delay_seconds=int(env.get("WELCOME_DELAY_SECONDS", "60")),
            welcome_folder=env.get("WELCOME_FOLDER", "Welcome"),
            welcome_text=env.get("WELCOME_TEXT", DEFAULT_WELCOME),
            welcome_text_overrides=overrides,
            poll_interval_seconds=int(env.get("POLL_INTERVAL_SECONDS", "30")),
            anchor_interval_seconds=int(env.get("ANCHOR_INTERVAL_SECONDS", "20")),
            anchor_timeout_minutes=int(env.get("ANCHOR_TIMEOUT_MINUTES", "30")),
            unanswered_threshold_minutes=int(env.get("UNANSWERED_THRESHOLD_MINUTES", "5")),
            unanswered_poll_interval_seconds=int(env.get("UNANSWERED_POLL_INTERVAL_SECONDS", "60")),
            unanswered_renotify_minutes=int(env.get("UNANSWERED_RENOTIFY_MINUTES", "0")),
            unanswered_subscribers_only=_bool(env.get("UNANSWERED_SUBSCRIBERS_ONLY"), False),
            watchdog_enabled=_bool(env.get("WATCHDOG_ENABLED"), True),
            db_path=env.get("DB_PATH", "/data/boltchatter.db"),
            dry_run=_bool(env.get("DRY_RUN"), False),
            health_port=int(env.get("PORT", "8080")),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/config.py tests/test_config.py
git commit -m "feat: env-driven Config with per-creator welcome overrides"
```

---

## Task 5: Store — schema, seen, meta

**Files:**
- Create: `boltchatter/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:
```python
from boltchatter.store import Store


def make_store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_meta_roundtrip():
    s = make_store()
    assert s.meta_get("k") is None
    s.meta_set("k", "v")
    assert s.meta_get("k") == "v"


def test_bootstrap_flag():
    s = make_store()
    assert s.bootstrap_done() is False
    s.mark_bootstrap_done()
    assert s.bootstrap_done() is True


def test_seen_add_and_has():
    s = make_store()
    assert s.seen_has("c1", "u1") is False
    s.seen_add("c1", "u1", "2026-06-22T12:00:00+00:00")
    assert s.seen_has("c1", "u1") is True
    # idempotent
    s.seen_add("c1", "u1", "2026-06-22T13:00:00+00:00")
    assert s.seen_has("c1", "u1") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/store.py`:
```python
from __future__ import annotations

import sqlite3
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
  creator_uuid TEXT NOT NULL,
  sub_uuid     TEXT NOT NULL,
  first_seen   TEXT NOT NULL,
  PRIMARY KEY (creator_uuid, sub_uuid)
);
CREATE TABLE IF NOT EXISTS welcome_jobs (
  creator_uuid TEXT NOT NULL,
  sub_uuid     TEXT NOT NULL,
  display_name TEXT,
  handle       TEXT,
  status       TEXT NOT NULL,
  fire_at      TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (creator_uuid, sub_uuid)
);
CREATE TABLE IF NOT EXISTS unanswered_watch (
  creator_uuid      TEXT NOT NULL,
  user_uuid         TEXT NOT NULL,
  last_message_uuid TEXT NOT NULL,
  handle            TEXT,
  display_name      TEXT,
  last_message_text TEXT,
  first_seen_at     TEXT NOT NULL,
  notified_at       TEXT,
  PRIMARY KEY (creator_uuid, user_uuid)
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- meta ----
    def meta_get(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def bootstrap_done(self) -> bool:
        return self.meta_get("bootstrap_done") == "true"

    def mark_bootstrap_done(self) -> None:
        self.meta_set("bootstrap_done", "true")

    # ---- seen ----
    def seen_has(self, creator_uuid: str, sub_uuid: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen WHERE creator_uuid=? AND sub_uuid=?",
            (creator_uuid, sub_uuid),
        ).fetchone()
        return row is not None

    def seen_add(self, creator_uuid: str, sub_uuid: str, first_seen: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen(creator_uuid, sub_uuid, first_seen) VALUES(?,?,?)",
            (creator_uuid, sub_uuid, first_seen),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/store.py tests/test_store.py
git commit -m "feat: Store with schema, meta, seen tables"
```

---

## Task 6: Store — welcome_jobs

**Files:**
- Modify: `boltchatter/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test (append to tests/test_store.py)**

```python
from boltchatter.models import Status


def test_job_lifecycle():
    s = make_store()
    s.job_create("c1", "u1", "Jess", "jess", "2026-06-22T12:00:00+00:00")
    job = s.job_get("c1", "u1")
    assert job["status"] == Status.AWAITING_GENERIC
    assert job["fire_at"] is None
    assert job["attempts"] == 0

    assert [j["sub_uuid"] for j in s.jobs_by_status(Status.AWAITING_GENERIC)] == ["u1"]

    s.job_set_pending("c1", "u1", "2026-06-22T12:01:00+00:00", "2026-06-22T12:00:30+00:00")
    job = s.job_get("c1", "u1")
    assert job["status"] == Status.PENDING
    assert job["fire_at"] == "2026-06-22T12:01:00+00:00"

    due = s.jobs_pending_due("2026-06-22T12:05:00+00:00")
    assert [j["sub_uuid"] for j in due] == ["u1"]
    assert s.jobs_pending_due("2026-06-22T12:00:00+00:00") == []

    s.job_set_status("c1", "u1", Status.SENT, "2026-06-22T12:05:01+00:00")
    assert s.job_get("c1", "u1")["status"] == Status.SENT
    assert s.jobs_by_status(Status.PENDING) == []


def test_job_bump_attempts():
    s = make_store()
    s.job_create("c1", "u1", "Jess", "jess", "2026-06-22T12:00:00+00:00")
    s.job_bump_attempts("c1", "u1", "2026-06-22T12:00:10+00:00")
    assert s.job_get("c1", "u1")["attempts"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py::test_job_lifecycle -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'job_create'`

- [ ] **Step 3: Add job methods to `boltchatter/store.py`**

Append inside the `Store` class:
```python
    # ---- welcome_jobs ----
    def job_create(self, creator_uuid, sub_uuid, display_name, handle, now) -> None:
        from .models import Status
        self._conn.execute(
            "INSERT OR IGNORE INTO welcome_jobs"
            "(creator_uuid, sub_uuid, display_name, handle, status, fire_at,"
            " attempts, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (creator_uuid, sub_uuid, display_name, handle,
             Status.AWAITING_GENERIC, None, 0, now, now),
        )
        self._conn.commit()

    def job_get(self, creator_uuid, sub_uuid):
        return self._conn.execute(
            "SELECT * FROM welcome_jobs WHERE creator_uuid=? AND sub_uuid=?",
            (creator_uuid, sub_uuid),
        ).fetchone()

    def jobs_by_status(self, status):
        return self._conn.execute(
            "SELECT * FROM welcome_jobs WHERE status=? ORDER BY created_at",
            (status,),
        ).fetchall()

    def jobs_pending_due(self, now):
        from .models import Status
        return self._conn.execute(
            "SELECT * FROM welcome_jobs WHERE status=? AND fire_at IS NOT NULL "
            "AND fire_at <= ? ORDER BY fire_at",
            (Status.PENDING, now),
        ).fetchall()

    def job_set_pending(self, creator_uuid, sub_uuid, fire_at, now):
        from .models import Status
        self._conn.execute(
            "UPDATE welcome_jobs SET status=?, fire_at=?, updated_at=? "
            "WHERE creator_uuid=? AND sub_uuid=?",
            (Status.PENDING, fire_at, now, creator_uuid, sub_uuid),
        )
        self._conn.commit()

    def job_set_status(self, creator_uuid, sub_uuid, status, now):
        self._conn.execute(
            "UPDATE welcome_jobs SET status=?, updated_at=? "
            "WHERE creator_uuid=? AND sub_uuid=?",
            (status, now, creator_uuid, sub_uuid),
        )
        self._conn.commit()

    def job_bump_attempts(self, creator_uuid, sub_uuid, now):
        self._conn.execute(
            "UPDATE welcome_jobs SET attempts=attempts+1, updated_at=? "
            "WHERE creator_uuid=? AND sub_uuid=?",
            (now, creator_uuid, sub_uuid),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (all store tests)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/store.py tests/test_store.py
git commit -m "feat: welcome_jobs CRUD on Store"
```

---

## Task 7: Store — unanswered_watch

**Files:**
- Modify: `boltchatter/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test (append to tests/test_store.py)**

```python
def test_watch_upsert_and_reset_on_new_message():
    s = make_store()
    s.watch_upsert("c1", "u1", "m1", "jess", "Jess", "hi", "2026-06-22T12:00:00+00:00")
    row = s.watch_get("c1", "u1")
    assert row["last_message_uuid"] == "m1"
    assert row["first_seen_at"] == "2026-06-22T12:00:00+00:00"
    assert row["notified_at"] is None

    # same message -> first_seen_at unchanged (no reset)
    s.watch_upsert("c1", "u1", "m1", "jess", "Jess", "hi", "2026-06-22T12:00:30+00:00")
    assert s.watch_get("c1", "u1")["first_seen_at"] == "2026-06-22T12:00:00+00:00"

    # new message -> reset anchor + clear notified
    s.watch_mark_notified("c1", "u1", "2026-06-22T12:06:00+00:00")
    s.watch_upsert("c1", "u1", "m2", "jess", "Jess", "you there?", "2026-06-22T12:07:00+00:00")
    row = s.watch_get("c1", "u1")
    assert row["last_message_uuid"] == "m2"
    assert row["first_seen_at"] == "2026-06-22T12:07:00+00:00"
    assert row["notified_at"] is None


def test_watch_all_keys_and_delete():
    s = make_store()
    s.watch_upsert("c1", "u1", "m1", "j", "J", "hi", "2026-06-22T12:00:00+00:00")
    s.watch_upsert("c1", "u2", "m9", "k", "K", "yo", "2026-06-22T12:00:00+00:00")
    assert {(r["creator_uuid"], r["user_uuid"]) for r in s.watch_all()} == {("c1", "u1"), ("c1", "u2")}
    s.watch_delete("c1", "u1")
    assert {(r["creator_uuid"], r["user_uuid"]) for r in s.watch_all()} == {("c1", "u2")}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py::test_watch_upsert_and_reset_on_new_message -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Add watch methods to `boltchatter/store.py`**

Append inside the `Store` class:
```python
    # ---- unanswered_watch ----
    def watch_get(self, creator_uuid, user_uuid):
        return self._conn.execute(
            "SELECT * FROM unanswered_watch WHERE creator_uuid=? AND user_uuid=?",
            (creator_uuid, user_uuid),
        ).fetchone()

    def watch_all(self):
        return self._conn.execute("SELECT * FROM unanswered_watch").fetchall()

    def watch_upsert(self, creator_uuid, user_uuid, last_message_uuid,
                     handle, display_name, last_message_text, now):
        existing = self.watch_get(creator_uuid, user_uuid)
        if existing is not None and existing["last_message_uuid"] == last_message_uuid:
            # same unanswered message: refresh text/name only, keep anchor + notified_at
            self._conn.execute(
                "UPDATE unanswered_watch SET handle=?, display_name=?, last_message_text=? "
                "WHERE creator_uuid=? AND user_uuid=?",
                (handle, display_name, last_message_text, creator_uuid, user_uuid),
            )
        else:
            # new row OR newer message: (re)anchor and clear notified_at
            self._conn.execute(
                "INSERT INTO unanswered_watch"
                "(creator_uuid, user_uuid, last_message_uuid, handle, display_name,"
                " last_message_text, first_seen_at, notified_at) VALUES(?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(creator_uuid, user_uuid) DO UPDATE SET "
                "last_message_uuid=excluded.last_message_uuid, handle=excluded.handle, "
                "display_name=excluded.display_name, last_message_text=excluded.last_message_text, "
                "first_seen_at=excluded.first_seen_at, notified_at=NULL",
                (creator_uuid, user_uuid, last_message_uuid, handle, display_name,
                 last_message_text, now),
            )
        self._conn.commit()

    def watch_mark_notified(self, creator_uuid, user_uuid, now):
        self._conn.execute(
            "UPDATE unanswered_watch SET notified_at=? WHERE creator_uuid=? AND user_uuid=?",
            (now, creator_uuid, user_uuid),
        )
        self._conn.commit()

    def watch_delete(self, creator_uuid, user_uuid):
        self._conn.execute(
            "DELETE FROM unanswered_watch WHERE creator_uuid=? AND user_uuid=?",
            (creator_uuid, user_uuid),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (all store tests)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/store.py tests/test_store.py
git commit -m "feat: unanswered_watch CRUD with anchor-reset semantics"
```

---

## Task 8: Telegram sender

**Files:**
- Create: `boltchatter/telegram.py`
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Write the failing test**

`tests/test_telegram.py`:
```python
import httpx
import pytest
from boltchatter.telegram import send_telegram, escape_html


def test_escape_html():
    assert escape_html("a<b>&'c") == "a&lt;b&gt;&amp;&#x27;c"


@pytest.mark.asyncio
async def test_send_posts_to_telegram():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await send_telegram("BOTTOKEN", "999", "<b>hi</b>", client=client)

    assert "botBOTTOKEN/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == "999"
    assert captured["body"]["parse_mode"] == "HTML"
    assert captured["body"]["text"] == "<b>hi</b>"


@pytest.mark.asyncio
async def test_send_noops_when_unconfigured():
    # Missing token/chat -> returns without raising and without a client call.
    await send_telegram("", "", "hi", client=None)


@pytest.mark.asyncio
async def test_send_swallows_errors():
    def handler(request):
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # must not raise
        await send_telegram("T", "1", "hi", client=client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_telegram.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/telegram.py`:
```python
from __future__ import annotations

import logging
import httpx

log = logging.getLogger("boltchatter.telegram")
TELEGRAM_API = "https://api.telegram.org"


def escape_html(value: object) -> str:
    s = str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


async def send_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Best-effort: never raises. No-ops if token/chat missing."""
    if not bot_token or not chat_id:
        log.warning("[Telegram] missing token/chat id, skipping notification")
        return
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        if client is not None:
            res = await client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                res = await c.post(url, json=payload)
        if res.status_code >= 400:
            log.warning("[Telegram] API %s: %s", res.status_code, res.text)
    except Exception as err:  # noqa: BLE001 - best effort
        log.warning("[Telegram] send failed: %r", err)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_telegram.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/telegram.py tests/test_telegram.py
git commit -m "feat: best-effort async Telegram sender (Papacito/Aurora pattern)"
```

---

## Task 9: Telegram alert formatters

**Files:**
- Modify: `boltchatter/telegram.py`
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Write the failing test (append to tests/test_telegram.py)**

```python
from boltchatter.telegram import format_new_sub, format_unanswered


def test_format_new_sub_escapes_and_labels():
    txt = format_new_sub(handle="je<ss", display_name="Jess", creator_label="Creator A")
    assert "New BoltChatter Sub" in txt
    assert "je&lt;ss" in txt
    assert "Creator A" in txt


def test_format_unanswered_includes_minutes_and_text():
    txt = format_unanswered(
        handle="jess", display_name="Jess", minutes=5,
        message_text="where you <at>?", creator_label=None,
    )
    assert "5m" in txt
    assert "where you &lt;at&gt;?" in txt
    assert "Reply" in txt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_telegram.py::test_format_new_sub_escapes_and_labels -v`
Expected: FAIL with `ImportError: cannot import name 'format_new_sub'`

- [ ] **Step 3: Add formatters to `boltchatter/telegram.py`**

```python
def _truncate(s: str, limit: int = 200) -> str:
    s = s or ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_new_sub(*, handle: str, display_name: str, creator_label: str | None) -> str:
    lines = [
        "\U0001F7E3 <b>New BoltChatter Sub!</b>",
        f"\U0001F464 {escape_html(display_name)} (@{escape_html(handle)})",
    ]
    if creator_label:
        lines.append(f"\U0001F3AC <b>Creator:</b> {escape_html(creator_label)}")
    return "\n".join(lines)


def format_unanswered(
    *, handle: str, display_name: str, minutes: int,
    message_text: str, creator_label: str | None,
) -> str:
    lines = [
        f"⏰ <b>Unanswered {minutes}m — reply needed!</b>",
        f"\U0001F464 {escape_html(display_name)} (@{escape_html(handle)})",
    ]
    if creator_label:
        lines.append(f"\U0001F3AC <b>Creator:</b> {escape_html(creator_label)}")
    lines.append(f"\U0001F4AC {escape_html(_truncate(message_text))}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_telegram.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/telegram.py tests/test_telegram.py
git commit -m "feat: Telegram alert formatters for new-sub and unanswered"
```

---

## Task 10: FanvueClient

**Files:**
- Create: `boltchatter/fanvue/client.py`
- Test: `tests/test_fanvue_client.py`

- [ ] **Step 1: Write the failing test**

`tests/test_fanvue_client.py`:
```python
import httpx
import pytest
from boltchatter.fanvue.client import FanvueClient


def make_client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.fanvue.com")
    return FanvueClient(token="TOK", api_version="2025-06-26", http=http)


@pytest.mark.asyncio
async def test_get_sends_auth_and_version_headers():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["ver"] = request.headers.get("x-fanvue-api-version")
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": 1})

    c = make_client(handler)
    out = await c.get("/subscribers", params={"page": 1})
    assert out == {"ok": 1}
    assert seen["auth"] == "Bearer TOK"
    assert seen["ver"] == "2025-06-26"
    assert seen["path"] == "/subscribers"
    assert seen["query"]["page"] == "1"
    await c.aclose()


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"done": True})

    c = make_client(handler)
    out = await c.get("/chats")
    assert out == {"done": True}
    assert calls["n"] == 2
    await c.aclose()


@pytest.mark.asyncio
async def test_post_sends_json_body():
    seen = {}

    def handler(request):
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messageUuid": "m1"})

    c = make_client(handler)
    out = await c.post("/chats/u1/message", json={"text": "hi"})
    assert out["messageUuid"] == "m1"
    assert seen["body"]["text"] == "hi"
    await c.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fanvue_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/fanvue/client.py`:
```python
from __future__ import annotations

import asyncio
import logging
import httpx

log = logging.getLogger("boltchatter.fanvue")
MAX_RETRIES = 4


class FanvueClient:
    def __init__(self, token: str, api_version: str, http: httpx.AsyncClient) -> None:
        self._http = http
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Fanvue-API-Version": api_version,
            "Accept": "application/json",
        }

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        for attempt in range(MAX_RETRIES):
            res = await self._http.request(
                method, path, params=params, json=json, headers=self._headers
            )
            if res.status_code == 429:
                retry_after = float(res.headers.get("Retry-After", "1"))
                log.warning("[Fanvue] 429 on %s, sleeping %ss", path, retry_after)
                await asyncio.sleep(retry_after)
                continue
            res.raise_for_status()
            if not res.content:
                return {}
            return res.json()
        raise RuntimeError(f"[Fanvue] exhausted retries for {method} {path}")

    async def get(self, path: str, *, params=None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json=None) -> dict:
        return await self._request("POST", path, json=json)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fanvue_client.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/fanvue/client.py tests/test_fanvue_client.py
git commit -m "feat: FanvueClient async wrapper with auth headers + 429 retry"
```

---

## Task 11: SubscriberSource protocol + sentinel

**Files:**
- Create: `boltchatter/fanvue/source.py`

- [ ] **Step 1: Write the protocol (no test — it's an interface definition exercised by Task 12)**

`boltchatter/fanvue/source.py`:
```python
from __future__ import annotations

from typing import Protocol
from ..models import NewSub, UnansweredChat

SELF = "self"  # creator_uuid sentinel used in single-creator mode


class SubscriberSource(Protocol):
    async def list_creators(self) -> list[str]: ...
    async def list_recent_subscribers(self) -> list[NewSub]: ...
    async def find_generic_welcome(self, creator_uuid: str, sub_uuid: str) -> bool: ...
    async def resolve_welcome_photo(self, creator_uuid: str) -> str | None: ...
    async def send_message(
        self, creator_uuid: str, sub_uuid: str, text: str, media_uuid: str | None
    ) -> str: ...
    async def list_unanswered_chats(self, creator_uuid: str) -> list[UnansweredChat]: ...
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from boltchatter.fanvue.source import SubscriberSource, SELF; print(SELF)"`
Expected: prints `self`

- [ ] **Step 3: Commit**

```bash
git add boltchatter/fanvue/source.py
git commit -m "feat: SubscriberSource protocol + SELF sentinel"
```

---

## Task 12: CreatorSource

**Files:**
- Create: `boltchatter/fanvue/creator_source.py`
- Test: `tests/test_sources.py`

**Note:** `AUTOMATED_NEW_SUBSCRIBER` is the message `type` for Fanvue's generic welcome.
The not_answered guard uses `lastMessage.senderUuid == user.uuid` to confirm the last
message is from the fan.

- [ ] **Step 1: Write the failing test**

`tests/test_sources.py`:
```python
import httpx
import pytest
from boltchatter.fanvue.client import FanvueClient
from boltchatter.fanvue.creator_source import CreatorSource
from boltchatter.fanvue.source import SELF


def make_source(routes):
    """routes: dict of (method, path) -> json dict (or callable(request)->Response)."""
    def handler(request):
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"error": "no route", "path": request.url.path})
        val = routes[key]
        if callable(val):
            return val(request)
        return httpx.Response(200, json=val)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.fanvue.com")
    client = FanvueClient("TOK", "2025-06-26", http)
    return CreatorSource(client), client


@pytest.mark.asyncio
async def test_list_creators_returns_self():
    src, client = make_source({})
    assert await src.list_creators() == [SELF]
    await client.aclose()


@pytest.mark.asyncio
async def test_list_recent_subscribers_maps_rows():
    routes = {
        ("GET", "/subscribers"): {
            "data": [
                {"uuid": "u1", "handle": "jess", "displayName": "Jess"},
                {"uuid": "u2", "handle": "kay", "displayName": "Kay"},
            ],
            "pagination": {"hasMore": False},
        }
    }
    src, client = make_source(routes)
    subs = await src.list_recent_subscribers()
    assert [(s.creator_uuid, s.sub_uuid, s.handle) for s in subs] == [
        (SELF, "u1", "jess"), (SELF, "u2", "kay"),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_find_generic_welcome_true_when_present():
    routes = {
        ("GET", "/chats/u1/messages"): {
            "data": [
                {"uuid": "m1", "type": "AUTOMATED_NEW_SUBSCRIBER", "text": "welcome"},
            ],
            "pagination": {"hasMore": False},
        }
    }
    src, client = make_source(routes)
    assert await src.find_generic_welcome(SELF, "u1") is True
    await client.aclose()


@pytest.mark.asyncio
async def test_find_generic_welcome_false_when_absent():
    routes = {
        ("GET", "/chats/u1/messages"): {
            "data": [{"uuid": "m1", "type": "SINGLE_RECIPIENT", "text": "hi"}],
            "pagination": {"hasMore": False},
        }
    }
    src, client = make_source(routes)
    assert await src.find_generic_welcome(SELF, "u1") is False
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_welcome_photo_returns_first_media_uuid():
    routes = {
        ("GET", "/vault/folders/Welcome/media"): {
            "data": [{"uuid": "media-1"}, {"uuid": "media-2"}],
            "pagination": {"hasMore": False},
        }
    }
    src, client = make_source(routes)
    assert await src.resolve_welcome_photo(SELF) == "media-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_welcome_photo_none_when_empty():
    routes = {("GET", "/vault/folders/Welcome/media"): {"data": [], "pagination": {}}}
    src, client = make_source(routes)
    assert await src.resolve_welcome_photo(SELF) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_send_message_free_with_media():
    captured = {}

    def post_handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messageUuid": "sent-1"})

    routes = {("POST", "/chats/u1/message"): post_handler}
    src, client = make_source(routes)
    out = await src.send_message(SELF, "u1", "hello", "media-1")
    assert out == "sent-1"
    assert captured["body"]["text"] == "hello"
    assert captured["body"]["mediaUuids"] == ["media-1"]
    assert captured["body"]["price"] is None
    await client.aclose()


@pytest.mark.asyncio
async def test_list_unanswered_filters_and_maps():
    routes = {
        ("GET", "/chats"): {
            "data": [
                {
                    "user": {"uuid": "u1", "handle": "jess", "displayName": "Jess"},
                    "lastMessage": {"uuid": "m1", "text": "you there?",
                                    "senderUuid": "u1"},
                },
                {  # last message from creator -> skipped by guard
                    "user": {"uuid": "u2", "handle": "kay", "displayName": "Kay"},
                    "lastMessage": {"uuid": "m2", "text": "ok!", "senderUuid": "creator-x"},
                },
                {  # no lastMessage -> skipped
                    "user": {"uuid": "u3", "handle": "lee", "displayName": "Lee"},
                    "lastMessage": None,
                },
            ],
            "pagination": {"hasMore": False},
        }
    }
    src, client = make_source(routes)
    chats = await src.list_unanswered_chats(SELF)
    assert [(c.user_uuid, c.last_message_uuid, c.last_message_text) for c in chats] == [
        ("u1", "m1", "you there?"),
    ]
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sources.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'boltchatter.fanvue.creator_source'`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/fanvue/creator_source.py`:
```python
from __future__ import annotations

from ..models import NewSub, UnansweredChat
from .client import FanvueClient
from .source import SELF

GENERIC_WELCOME_TYPE = "AUTOMATED_NEW_SUBSCRIBER"


class CreatorSource:
    """Single-creator token. creator_uuid is always SELF; endpoints are non-scoped."""

    def __init__(self, client: FanvueClient, *, welcome_folder: str = "Welcome",
                 unanswered_subscribers_only: bool = False) -> None:
        self._c = client
        self._welcome_folder = welcome_folder
        self._subs_only = unanswered_subscribers_only

    async def list_creators(self) -> list[str]:
        return [SELF]

    async def list_recent_subscribers(self) -> list[NewSub]:
        out = await self._c.get(
            "/subscribers",
            params={"sortField": "subscribedAt", "sortDirection": "desc", "size": 50},
        )
        return [
            NewSub(
                creator_uuid=SELF,
                sub_uuid=row["uuid"],
                display_name=row.get("displayName") or "",
                handle=row.get("handle") or "",
            )
            for row in out.get("data", [])
        ]

    async def find_generic_welcome(self, creator_uuid: str, sub_uuid: str) -> bool:
        out = await self._c.get(f"/chats/{sub_uuid}/messages", params={"size": 50})
        return any(m.get("type") == GENERIC_WELCOME_TYPE for m in out.get("data", []))

    async def resolve_welcome_photo(self, creator_uuid: str) -> str | None:
        out = await self._c.get(f"/vault/folders/{self._welcome_folder}/media",
                                params={"size": 50})
        data = out.get("data", [])
        return data[0]["uuid"] if data else None

    async def send_message(self, creator_uuid, sub_uuid, text, media_uuid) -> str:
        body = {
            "text": text,
            "mediaUuids": [media_uuid] if media_uuid else [],
            "price": None,
        }
        out = await self._c.post(f"/chats/{sub_uuid}/message", json=body)
        return out.get("messageUuid", "")

    async def list_unanswered_chats(self, creator_uuid: str) -> list[UnansweredChat]:
        params = {"filter": ["not_answered"], "sortBy": "most_recent_messages", "size": 50}
        if self._subs_only:
            params["filter"] = ["not_answered", "subscribers"]
        out = await self._c.get("/chats", params=params)
        return _map_unanswered(out.get("data", []), creator_uuid)


def _map_unanswered(rows, creator_uuid):
    result = []
    for row in rows:
        user = row.get("user") or {}
        last = row.get("lastMessage")
        if not last:
            continue
        # guard: last message must be from the fan, not the operator
        if last.get("senderUuid") and last.get("senderUuid") != user.get("uuid"):
            continue
        result.append(UnansweredChat(
            creator_uuid=creator_uuid,
            user_uuid=user.get("uuid", ""),
            handle=user.get("handle") or "",
            display_name=user.get("displayName") or "",
            last_message_uuid=last.get("uuid", ""),
            last_message_text=last.get("text") or "",
        ))
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sources.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/fanvue/creator_source.py tests/test_sources.py
git commit -m "feat: CreatorSource (single-creator mode)"
```

---

## Task 13: AgencySource

**Files:**
- Create: `boltchatter/fanvue/agency_source.py`
- Test: `tests/test_sources.py`

- [ ] **Step 1: Write the failing test (append to tests/test_sources.py)**

```python
from boltchatter.fanvue.agency_source import AgencySource


def make_agency(routes):
    def handler(request):
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"path": request.url.path})
        val = routes[key]
        return val(request) if callable(val) else httpx.Response(200, json=val)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.fanvue.com")
    client = FanvueClient("TOK", "2025-06-26", http)
    return AgencySource(client), client


@pytest.mark.asyncio
async def test_agency_list_creators():
    routes = {("GET", "/creators"): {"data": [{"uuid": "c1"}, {"uuid": "c2"}], "pagination": {}}}
    src, client = make_agency(routes)
    assert await src.list_creators() == ["c1", "c2"]
    await client.aclose()


@pytest.mark.asyncio
async def test_agency_subscribers_carry_creator_uuid():
    routes = {("GET", "/agencies/subscribers"): {
        "data": [
            {"uuid": "u1", "handle": "jess", "displayName": "Jess", "creatorUuid": "c1"},
            {"uuid": "u2", "handle": "kay", "displayName": "Kay", "creatorUuid": "c2"},
        ],
        "pagination": {"hasMore": False},
    }}
    src, client = make_agency(routes)
    subs = await src.list_recent_subscribers()
    assert [(s.creator_uuid, s.sub_uuid) for s in subs] == [("c1", "u1"), ("c2", "u2")]
    await client.aclose()


@pytest.mark.asyncio
async def test_agency_find_generic_uses_creator_scoped_path():
    routes = {("GET", "/creators/c1/chats/u1/messages"): {
        "data": [{"uuid": "m1", "type": "AUTOMATED_NEW_SUBSCRIBER"}], "pagination": {}}}
    src, client = make_agency(routes)
    assert await src.find_generic_welcome("c1", "u1") is True
    await client.aclose()


@pytest.mark.asyncio
async def test_agency_send_uses_creator_scoped_path():
    captured = {}

    def post_handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messageUuid": "s1"})

    routes = {("POST", "/creators/c1/chats/u1/message"): post_handler}
    src, client = make_agency(routes)
    out = await src.send_message("c1", "u1", "hi", "media-1")
    assert out == "s1"
    assert captured["body"]["mediaUuids"] == ["media-1"]
    await client.aclose()


@pytest.mark.asyncio
async def test_agency_unanswered_uses_creator_scoped_path():
    routes = {("GET", "/creators/c1/chats"): {
        "data": [{"user": {"uuid": "u1", "handle": "j", "displayName": "J"},
                  "lastMessage": {"uuid": "m1", "text": "yo", "senderUuid": "u1"}}],
        "pagination": {}}}
    src, client = make_agency(routes)
    chats = await src.list_unanswered_chats("c1")
    assert [c.user_uuid for c in chats] == ["u1"]
    assert chats[0].creator_uuid == "c1"
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sources.py -k agency -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'boltchatter.fanvue.agency_source'`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/fanvue/agency_source.py`:
```python
from __future__ import annotations

from ..models import NewSub, UnansweredChat
from .client import FanvueClient
from .creator_source import GENERIC_WELCOME_TYPE, _map_unanswered


class AgencySource:
    """Agency token. Real creator UUIDs; creator-scoped endpoints."""

    def __init__(self, client: FanvueClient, *, welcome_folder: str = "Welcome",
                 unanswered_subscribers_only: bool = False) -> None:
        self._c = client
        self._welcome_folder = welcome_folder
        self._subs_only = unanswered_subscribers_only

    async def list_creators(self) -> list[str]:
        out = await self._c.get("/creators", params={"size": 50})
        return [row["uuid"] for row in out.get("data", [])]

    async def list_recent_subscribers(self) -> list[NewSub]:
        out = await self._c.get("/agencies/subscribers", params={"size": 50})
        return [
            NewSub(
                creator_uuid=row["creatorUuid"],
                sub_uuid=row["uuid"],
                display_name=row.get("displayName") or "",
                handle=row.get("handle") or "",
            )
            for row in out.get("data", [])
        ]

    async def find_generic_welcome(self, creator_uuid: str, sub_uuid: str) -> bool:
        out = await self._c.get(
            f"/creators/{creator_uuid}/chats/{sub_uuid}/messages", params={"size": 50}
        )
        return any(m.get("type") == GENERIC_WELCOME_TYPE for m in out.get("data", []))

    async def resolve_welcome_photo(self, creator_uuid: str) -> str | None:
        out = await self._c.get(
            f"/creators/{creator_uuid}/vault/folders/{self._welcome_folder}/media",
            params={"size": 50},
        )
        data = out.get("data", [])
        return data[0]["uuid"] if data else None

    async def send_message(self, creator_uuid, sub_uuid, text, media_uuid) -> str:
        body = {"text": text, "mediaUuids": [media_uuid] if media_uuid else [], "price": None}
        out = await self._c.post(
            f"/creators/{creator_uuid}/chats/{sub_uuid}/message", json=body
        )
        return out.get("messageUuid", "")

    async def list_unanswered_chats(self, creator_uuid: str) -> list[UnansweredChat]:
        params = {"filter": ["not_answered"], "sortBy": "most_recent_messages", "size": 50}
        if self._subs_only:
            params["filter"] = ["not_answered", "subscribers"]
        out = await self._c.get(f"/creators/{creator_uuid}/chats", params=params)
        return _map_unanswered(out.get("data", []), creator_uuid)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sources.py -v`
Expected: PASS (all source tests)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/fanvue/agency_source.py tests/test_sources.py
git commit -m "feat: AgencySource (agency mode, creator-scoped endpoints)"
```

---

## Task 14: Shared test fixtures + FakeSource

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Write the fixtures (exercised by Tasks 15-18)**

`tests/conftest.py`:
```python
from datetime import datetime, timezone
import pytest

from boltchatter.store import Store
from boltchatter.clock import FakeClock
from boltchatter.models import NewSub, UnansweredChat


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    yield s
    s.close()


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc))


class FakeSource:
    """In-memory SubscriberSource for loop tests. Fully scriptable."""

    def __init__(self):
        self.creators = ["self"]
        self.recent_subs: list[NewSub] = []
        self.generic_present: set[tuple[str, str]] = set()   # (creator, sub) where welcome exists
        self.photo: dict[str, str | None] = {}               # creator -> media uuid
        self.unanswered: dict[str, list[UnansweredChat]] = {}  # creator -> chats
        self.sent: list[tuple] = []                          # (creator, sub, text, media)

    async def list_creators(self):
        return list(self.creators)

    async def list_recent_subscribers(self):
        return list(self.recent_subs)

    async def find_generic_welcome(self, creator_uuid, sub_uuid):
        return (creator_uuid, sub_uuid) in self.generic_present

    async def resolve_welcome_photo(self, creator_uuid):
        return self.photo.get(creator_uuid)

    async def send_message(self, creator_uuid, sub_uuid, text, media_uuid):
        self.sent.append((creator_uuid, sub_uuid, text, media_uuid))
        return "sent-uuid"

    async def list_unanswered_chats(self, creator_uuid):
        return list(self.unanswered.get(creator_uuid, []))


@pytest.fixture
def source():
    return FakeSource()


@pytest.fixture
def notifications():
    """Collects Telegram messages instead of sending them."""
    sent = []

    async def notify(text: str):
        sent.append(text)

    notify.sent = sent  # type: ignore[attr-defined]
    return notify
```

- [ ] **Step 2: Verify fixtures import**

Run: `python -m pytest tests/conftest.py -v` (collects nothing but must not error)
Expected: no collection errors

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: shared fixtures (in-memory Store, FakeClock, FakeSource, notify collector)"
```

---

## Task 15: Poller loop (detection + cold-start bootstrap)

**Files:**
- Create: `boltchatter/loops/poller.py`
- Test: `tests/test_poller.py`

The poller's `notify` argument is an `async (text) -> None` callable; the app wires it to
Telegram, tests wire it to a collector.

**Scope note (single-page fetch):** `list_recent_subscribers` fetches one page of 50
newest subs per poll. At a 30s interval this only misses subscribers if >50 people subscribe
in a single 30s window — not realistic for this bot. The spec's watermark-pagination edge case
is therefore deferred; if a high-volume creator ever needs it, add a `since`-watermark loop in
the source's `list_recent_subscribers` and a test for the >50 case. Logged here so the omission
is deliberate, not accidental.

- [ ] **Step 1: Write the failing test**

`tests/test_poller.py`:
```python
import pytest
from boltchatter.loops.poller import run_once
from boltchatter.models import NewSub, Status


@pytest.mark.asyncio
async def test_bootstrap_seeds_without_jobs_or_notifications(store, clock, source, notifications):
    source.recent_subs = [NewSub("self", "u1", "Jess", "jess"),
                          NewSub("self", "u2", "Kay", "kay")]
    await run_once(store, source, clock, notifications, creator_label=lambda c: None)

    # all existing subs marked seen, but NO jobs, NO telegram pings
    assert store.seen_has("self", "u1") and store.seen_has("self", "u2")
    assert store.jobs_by_status(Status.AWAITING_GENERIC) == []
    assert notifications.sent == []
    assert store.bootstrap_done() is True


@pytest.mark.asyncio
async def test_new_sub_after_bootstrap_creates_job_and_notifies(store, clock, source, notifications):
    # first run bootstraps with u1 only
    source.recent_subs = [NewSub("self", "u1", "Jess", "jess")]
    await run_once(store, source, clock, notifications, creator_label=lambda c: None)
    assert notifications.sent == []

    # u2 appears -> detected as new
    source.recent_subs = [NewSub("self", "u2", "Kay", "kay"),
                          NewSub("self", "u1", "Jess", "jess")]
    await run_once(store, source, clock, notifications, creator_label=lambda c: None)

    jobs = store.jobs_by_status(Status.AWAITING_GENERIC)
    assert [j["sub_uuid"] for j in jobs] == ["u2"]
    assert store.seen_has("self", "u2")
    assert len(notifications.sent) == 1
    assert "@kay" in notifications.sent[0]


@pytest.mark.asyncio
async def test_already_seen_sub_is_not_reprocessed(store, clock, source, notifications):
    source.recent_subs = [NewSub("self", "u1", "Jess", "jess")]
    await run_once(store, source, clock, notifications, creator_label=lambda c: None)  # bootstrap
    await run_once(store, source, clock, notifications, creator_label=lambda c: None)  # no change
    assert notifications.sent == []
    assert store.jobs_by_status(Status.AWAITING_GENERIC) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_poller.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/loops/poller.py`:
```python
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from ..clock import Clock
from ..store import Store
from ..telegram import format_new_sub

log = logging.getLogger("boltchatter.poller")

CreatorLabel = Callable[[str], "str | None"]
Notify = Callable[[str], Awaitable[None]]


async def run_once(store: Store, source, clock: Clock, notify: Notify,
                   creator_label: CreatorLabel) -> None:
    now = clock.now().isoformat()
    subs = await source.list_recent_subscribers()

    if not store.bootstrap_done():
        for s in subs:
            store.seen_add(s.creator_uuid, s.sub_uuid, now)
        store.mark_bootstrap_done()
        log.info("[poller] bootstrap seeded %d existing subscribers", len(subs))
        return

    for s in subs:
        if store.seen_has(s.creator_uuid, s.sub_uuid):
            continue
        store.seen_add(s.creator_uuid, s.sub_uuid, now)
        store.job_create(s.creator_uuid, s.sub_uuid, s.display_name, s.handle, now)
        text = format_new_sub(
            handle=s.handle, display_name=s.display_name,
            creator_label=creator_label(s.creator_uuid),
        )
        await notify(text)
        log.info("[poller] new subscriber %s (creator %s)", s.handle, s.creator_uuid)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_poller.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/loops/poller.py tests/test_poller.py
git commit -m "feat: poller loop with cold-start bootstrap + new-sub detection"
```

---

## Task 16: Anchor loop

**Files:**
- Create: `boltchatter/loops/anchor.py`
- Test: `tests/test_anchor.py`

- [ ] **Step 1: Write the failing test**

`tests/test_anchor.py`:
```python
import pytest
from boltchatter.loops.anchor import run_once
from boltchatter.models import NewSub, Status


def seed_job(store, clock):
    now = clock.now().isoformat()
    store.job_create("self", "u1", "Jess", "jess", now)


@pytest.mark.asyncio
async def test_no_generic_yet_stays_awaiting(store, clock, source):
    seed_job(store, clock)
    await run_once(store, source, clock, welcome_delay_seconds=60, anchor_timeout_minutes=30)
    assert store.job_get("self", "u1")["status"] == Status.AWAITING_GENERIC


@pytest.mark.asyncio
async def test_generic_present_moves_to_pending_with_fire_at(store, clock, source):
    seed_job(store, clock)
    source.generic_present.add(("self", "u1"))
    await run_once(store, source, clock, welcome_delay_seconds=60, anchor_timeout_minutes=30)
    job = store.job_get("self", "u1")
    assert job["status"] == Status.PENDING
    # fire_at == now + 60s
    assert job["fire_at"] == "2026-06-22T12:01:00+00:00"


@pytest.mark.asyncio
async def test_times_out_to_expired(store, clock, source):
    seed_job(store, clock)
    clock.advance(minutes=31)  # past anchor_timeout
    await run_once(store, source, clock, welcome_delay_seconds=60, anchor_timeout_minutes=30)
    assert store.job_get("self", "u1")["status"] == Status.EXPIRED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_anchor.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/loops/anchor.py`:
```python
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..clock import Clock
from ..models import Status
from ..store import Store

log = logging.getLogger("boltchatter.anchor")


async def run_once(store: Store, source, clock: Clock, *,
                   welcome_delay_seconds: int, anchor_timeout_minutes: int) -> None:
    now = clock.now()
    for job in store.jobs_by_status(Status.AWAITING_GENERIC):
        creator, sub = job["creator_uuid"], job["sub_uuid"]
        created = datetime.fromisoformat(job["created_at"])
        if now - created > timedelta(minutes=anchor_timeout_minutes):
            store.job_set_status(creator, sub, Status.EXPIRED, now.isoformat())
            log.warning("[anchor] job %s/%s expired (no generic welcome)", creator, sub)
            continue
        if await source.find_generic_welcome(creator, sub):
            fire_at = (now + timedelta(seconds=welcome_delay_seconds)).isoformat()
            store.job_set_pending(creator, sub, fire_at, now.isoformat())
            log.info("[anchor] job %s/%s armed, fire_at=%s", creator, sub, fire_at)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_anchor.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/loops/anchor.py tests/test_anchor.py
git commit -m "feat: anchor loop arms welcome 60s after generic welcome detected"
```

---

## Task 17: Scheduler loop

**Files:**
- Create: `boltchatter/loops/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

`tests/test_scheduler.py`:
```python
import pytest
from boltchatter.loops.scheduler import run_once
from boltchatter.models import Status


def arm_job(store, clock, fire_at):
    now = clock.now().isoformat()
    store.job_create("self", "u1", "Jess", "jess", now)
    store.job_set_pending("self", "u1", fire_at, now)


def welcome_text_for(creator):  # simple resolver used in tests
    return "hi {name}"


@pytest.mark.asyncio
async def test_not_due_does_not_send(store, clock, source):
    arm_job(store, clock, "2026-06-22T12:05:00+00:00")  # future
    await run_once(store, source, clock, welcome_text_for=welcome_text_for, max_attempts=3)
    assert source.sent == []
    assert store.job_get("self", "u1")["status"] == Status.PENDING


@pytest.mark.asyncio
async def test_due_sends_with_photo_and_name_and_marks_sent(store, clock, source):
    arm_job(store, clock, "2026-06-22T12:00:00+00:00")  # due now
    source.photo["self"] = "media-1"
    await run_once(store, source, clock, welcome_text_for=welcome_text_for, max_attempts=3)
    assert source.sent == [("self", "u1", "hi Jess", "media-1")]
    assert store.job_get("self", "u1")["status"] == Status.SENT


@pytest.mark.asyncio
async def test_send_failure_bumps_attempts_then_fails(store, clock, source):
    arm_job(store, clock, "2026-06-22T12:00:00+00:00")

    async def boom(*a, **k):
        raise RuntimeError("api down")
    source.send_message = boom  # type: ignore[assignment]

    # attempts 1 and 2 keep it PENDING
    await run_once(store, source, clock, welcome_text_for=welcome_text_for, max_attempts=3)
    assert store.job_get("self", "u1")["status"] == Status.PENDING
    assert store.job_get("self", "u1")["attempts"] == 1
    await run_once(store, source, clock, welcome_text_for=welcome_text_for, max_attempts=3)
    assert store.job_get("self", "u1")["attempts"] == 2
    # third attempt hits max -> FAILED
    await run_once(store, source, clock, welcome_text_for=welcome_text_for, max_attempts=3)
    assert store.job_get("self", "u1")["status"] == Status.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/loops/scheduler.py`:
```python
from __future__ import annotations

import logging
from typing import Callable

from ..clock import Clock
from ..models import Status
from ..store import Store

log = logging.getLogger("boltchatter.scheduler")


def _render(template: str, display_name: str) -> str:
    name = display_name.strip() or "love"
    return template.replace("{name}", name)


async def run_once(store: Store, source, clock: Clock, *,
                   welcome_text_for: Callable[[str], str], max_attempts: int) -> None:
    now = clock.now().isoformat()
    for job in store.jobs_pending_due(now):
        creator, sub = job["creator_uuid"], job["sub_uuid"]
        try:
            media = await source.resolve_welcome_photo(creator)
            text = _render(welcome_text_for(creator), job["display_name"] or "")
            await source.send_message(creator, sub, text, media)
            store.job_set_status(creator, sub, Status.SENT, now)
            log.info("[scheduler] welcome sent to %s/%s", creator, sub)
        except Exception as err:  # noqa: BLE001
            store.job_bump_attempts(creator, sub, now)
            attempts = store.job_get(creator, sub)["attempts"]
            log.warning("[scheduler] send failed (%d/%d) for %s/%s: %r",
                        attempts, max_attempts, creator, sub, err)
            if attempts >= max_attempts:
                store.job_set_status(creator, sub, Status.FAILED, now)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/loops/scheduler.py tests/test_scheduler.py
git commit -m "feat: scheduler loop sends welcome + photo with retry/fail"
```

---

## Task 18: Watchdog loop

**Files:**
- Create: `boltchatter/loops/watchdog.py`
- Test: `tests/test_watchdog.py`

- [ ] **Step 1: Write the failing test**

`tests/test_watchdog.py`:
```python
import pytest
from boltchatter.loops.watchdog import run_once
from boltchatter.models import UnansweredChat


def chat(user, msg, text="you there?"):
    return UnansweredChat("self", user, user, user.title(), msg, text)


async def tick(store, clock, source, notify, threshold=5, renotify=0):
    await run_once(store, source, clock, notify,
                   threshold_minutes=threshold, renotify_minutes=renotify,
                   creator_label=lambda c: None)


@pytest.mark.asyncio
async def test_under_threshold_no_alert(store, clock, source, notifications):
    source.unanswered = {"self": [chat("u1", "m1")]}
    await tick(store, clock, source, notifications)        # first seen at 12:00
    clock.advance(minutes=4)
    await tick(store, clock, source, notifications)        # only 4 min later
    assert notifications.sent == []


@pytest.mark.asyncio
async def test_crosses_threshold_alerts_once(store, clock, source, notifications):
    source.unanswered = {"self": [chat("u1", "m1", "where you at?")]}
    await tick(store, clock, source, notifications)        # anchor 12:00
    clock.advance(minutes=5)
    await tick(store, clock, source, notifications)        # 12:05 -> alert
    assert len(notifications.sent) == 1
    assert "@u1" in notifications.sent[0]
    assert "where you at?" in notifications.sent[0]
    # subsequent ticks do not re-alert (renotify=0)
    clock.advance(minutes=5)
    await tick(store, clock, source, notifications)
    assert len(notifications.sent) == 1


@pytest.mark.asyncio
async def test_new_message_resets_and_realerts(store, clock, source, notifications):
    source.unanswered = {"self": [chat("u1", "m1")]}
    await tick(store, clock, source, notifications)
    clock.advance(minutes=5)
    await tick(store, clock, source, notifications)
    assert len(notifications.sent) == 1
    # fan sends a newer message -> new last_message_uuid resets anchor
    source.unanswered = {"self": [chat("u1", "m2", "still waiting")]}
    await tick(store, clock, source, notifications)        # re-anchored, not yet 5 min
    assert len(notifications.sent) == 1
    clock.advance(minutes=5)
    await tick(store, clock, source, notifications)        # alert again
    assert len(notifications.sent) == 2


@pytest.mark.asyncio
async def test_answered_chat_is_reaped(store, clock, source, notifications):
    source.unanswered = {"self": [chat("u1", "m1")]}
    await tick(store, clock, source, notifications)
    assert store.watch_get("self", "u1") is not None
    # operator replied -> chat no longer in not_answered
    source.unanswered = {"self": []}
    await tick(store, clock, source, notifications)
    assert store.watch_get("self", "u1") is None


@pytest.mark.asyncio
async def test_renotify_repeats_after_interval(store, clock, source, notifications):
    source.unanswered = {"self": [chat("u1", "m1")]}
    await tick(store, clock, source, notifications, renotify=10)
    clock.advance(minutes=5)
    await tick(store, clock, source, notifications, renotify=10)   # first alert
    assert len(notifications.sent) == 1
    clock.advance(minutes=10)
    await tick(store, clock, source, notifications, renotify=10)   # renotify
    assert len(notifications.sent) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_watchdog.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/loops/watchdog.py`:
```python
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from ..clock import Clock
from ..store import Store
from ..telegram import format_unanswered

log = logging.getLogger("boltchatter.watchdog")

Notify = Callable[[str], Awaitable[None]]
CreatorLabel = Callable[[str], "str | None"]


async def run_once(store: Store, source, clock: Clock, notify: Notify, *,
                   threshold_minutes: int, renotify_minutes: int,
                   creator_label: CreatorLabel) -> None:
    now = clock.now()
    now_iso = now.isoformat()

    # 1) refresh watch rows from the live not_answered lists
    active: set[tuple[str, str]] = set()
    for creator in await source.list_creators():
        for c in await source.list_unanswered_chats(creator):
            active.add((c.creator_uuid, c.user_uuid))
            store.watch_upsert(c.creator_uuid, c.user_uuid, c.last_message_uuid,
                               c.handle, c.display_name, c.last_message_text, now_iso)

    # 2) reap rows that are no longer unanswered (operator replied)
    for row in store.watch_all():
        if (row["creator_uuid"], row["user_uuid"]) not in active:
            store.watch_delete(row["creator_uuid"], row["user_uuid"])

    # 3) alert pass
    threshold = timedelta(minutes=threshold_minutes)
    for row in store.watch_all():
        first_seen = datetime.fromisoformat(row["first_seen_at"])
        elapsed = now - first_seen
        if elapsed < threshold:
            continue
        notified_at = row["notified_at"]
        should_alert = notified_at is None
        if not should_alert and renotify_minutes > 0:
            last = datetime.fromisoformat(notified_at)
            should_alert = (now - last) >= timedelta(minutes=renotify_minutes)
        if not should_alert:
            continue
        minutes = int(elapsed.total_seconds() // 60)
        text = format_unanswered(
            handle=row["handle"] or "", display_name=row["display_name"] or "",
            minutes=minutes, message_text=row["last_message_text"] or "",
            creator_label=creator_label(row["creator_uuid"]),
        )
        await notify(text)
        store.watch_mark_notified(row["creator_uuid"], row["user_uuid"], now_iso)
        log.info("[watchdog] alerted unanswered %s/%s (%dm)",
                 row["creator_uuid"], row["user_uuid"], minutes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_watchdog.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/loops/watchdog.py tests/test_watchdog.py
git commit -m "feat: unanswered-chat watchdog loop with reap + optional renotify"
```

---

## Task 19: Health endpoint

**Files:**
- Create: `boltchatter/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write the failing test**

`tests/test_health.py`:
```python
import asyncio
import httpx
import pytest
from boltchatter.health import start_health_server


@pytest.mark.asyncio
async def test_health_returns_200():
    server = await start_health_server(0)  # port 0 = ephemeral
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"http://127.0.0.1:{port}/health")
        assert res.status_code == 200
        assert res.text == "ok"
    finally:
        server.close()
        await server.wait_closed()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_health.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/health.py`:
```python
from __future__ import annotations

import asyncio


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.readline()  # request line; we don't care about the path
        body = b"ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
    finally:
        writer.close()


async def start_health_server(port: int) -> asyncio.AbstractServer:
    return await asyncio.start_server(_handle, "0.0.0.0", port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_health.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add boltchatter/health.py tests/test_health.py
git commit -m "feat: minimal asyncio /health server for Railway"
```

---

## Task 20: App wiring (main)

**Files:**
- Create: `boltchatter/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

`tests/test_app.py`:
```python
import httpx
import pytest
from boltchatter.app import build_source, make_notify
from boltchatter.config import Config
from boltchatter.fanvue.creator_source import CreatorSource
from boltchatter.fanvue.agency_source import AgencySource


def cfg(**over):
    env = {
        "FANVUE_API_TOKEN": "tok", "FANVUE_MODE": over.pop("mode", "creator"),
        "TELEGRAM_BOT_TOKEN": "b", "TELEGRAM_CHAT_ID": "1",
    }
    env.update(over)
    return Config.from_env(env)


def test_build_source_creator():
    c = cfg(mode="creator")
    http = httpx.AsyncClient(base_url="https://api.fanvue.com")
    src = build_source(c, http)
    assert isinstance(src, CreatorSource)


def test_build_source_agency():
    c = cfg(mode="agency")
    http = httpx.AsyncClient(base_url="https://api.fanvue.com")
    src = build_source(c, http)
    assert isinstance(src, AgencySource)


@pytest.mark.asyncio
async def test_make_notify_dry_run_does_not_call_telegram(capsys):
    c = cfg(DRY_RUN="true")
    notify = make_notify(c, client=None)
    await notify("hello world")  # must not raise, must not POST
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`boltchatter/app.py`:
```python
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from .config import Config
from .clock import RealClock
from .store import Store
from .telegram import send_telegram
from .health import start_health_server
from .fanvue.client import FanvueClient
from .fanvue.creator_source import CreatorSource
from .fanvue.agency_source import AgencySource
from .loops import poller, anchor, scheduler, watchdog

log = logging.getLogger("boltchatter")
MAX_SEND_ATTEMPTS = 3


def build_source(cfg: Config, http: httpx.AsyncClient):
    client = FanvueClient(cfg.fanvue_token, cfg.api_version, http)
    kwargs = dict(welcome_folder=cfg.welcome_folder,
                  unanswered_subscribers_only=cfg.unanswered_subscribers_only)
    return AgencySource(client, **kwargs) if cfg.mode == "agency" else CreatorSource(client, **kwargs)


def make_notify(cfg: Config, *, client: httpx.AsyncClient | None) -> Callable[[str], Awaitable[None]]:
    async def notify(text: str) -> None:
        if cfg.dry_run:
            log.info("[DRY_RUN telegram] %s", text.replace("\n", " | "))
            return
        await send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, text, client=client)
    return notify


async def _loop(name: str, interval: int, fn) -> None:
    while True:
        try:
            await fn()
        except Exception as err:  # noqa: BLE001
            log.exception("[%s] loop iteration failed: %r", name, err)
        await asyncio.sleep(interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()

    store = Store(cfg.db_path)
    store.init_schema()
    clock = RealClock()

    fanvue_http = httpx.AsyncClient(base_url="https://api.fanvue.com", timeout=30)
    tg_http = httpx.AsyncClient(timeout=15)
    source = build_source(cfg, fanvue_http)
    notify = make_notify(cfg, client=tg_http)

    # creator label: in agency mode show the creator uuid; in creator mode no label
    def creator_label(creator_uuid: str):
        return None if cfg.mode == "creator" else creator_uuid

    await start_health_server(cfg.health_port)
    log.info("[boltchatter] started in %s mode (dry_run=%s)", cfg.mode, cfg.dry_run)

    tasks = [
        _loop("poller", cfg.poll_interval_seconds,
              lambda: poller.run_once(store, source, clock, notify, creator_label)),
        _loop("anchor", cfg.anchor_interval_seconds,
              lambda: anchor.run_once(store, source, clock,
                                      welcome_delay_seconds=cfg.welcome_delay_seconds,
                                      anchor_timeout_minutes=cfg.anchor_timeout_minutes)),
        _loop("scheduler", 10,
              lambda: scheduler.run_once(store, source, clock,
                                         welcome_text_for=cfg.welcome_text_for,
                                         max_attempts=MAX_SEND_ATTEMPTS)),
    ]
    if cfg.watchdog_enabled:
        tasks.append(_loop("watchdog", cfg.unanswered_poll_interval_seconds,
                           lambda: watchdog.run_once(store, source, clock, notify,
                                                     threshold_minutes=cfg.unanswered_threshold_minutes,
                                                     renotify_minutes=cfg.unanswered_renotify_minutes,
                                                     creator_label=creator_label)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add boltchatter/app.py boltchatter/loops/__init__.py tests/test_app.py
git commit -m "feat: app wiring, loop supervisor, dry-run notify"
```

---

## Task 21: Deployment artifacts + README

**Files:**
- Create: `Dockerfile`
- Create: `.env.example`
- Create: `README.md`

- [ ] **Step 1: Create `Dockerfile`**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY boltchatter ./boltchatter
RUN pip install --no-cache-dir .
# Railway provides $PORT; the health server reads it via Config.
CMD ["python", "-m", "boltchatter.app"]
```

- [ ] **Step 2: Create `.env.example`**

```bash
# --- required ---
FANVUE_API_TOKEN=your_fanvue_oauth_access_token
FANVUE_MODE=creator              # or: agency
TELEGRAM_BOT_TOKEN=papacito_bot_token   # reuse the shared Papacito/Aurora bot
TELEGRAM_CHAT_ID=papacito_chat_id       # reuse the shared chat

# --- optional (defaults shown) ---
FANVUE_API_VERSION=2025-06-26
WELCOME_DELAY_SECONDS=60
WELCOME_FOLDER=Welcome
# WELCOME_TEXT=Love, so happy you actually came! ... 😈
# WELCOME_TEXT_<creatorUuid>=per-creator override (agency mode)
POLL_INTERVAL_SECONDS=30
ANCHOR_INTERVAL_SECONDS=20
ANCHOR_TIMEOUT_MINUTES=30
UNANSWERED_THRESHOLD_MINUTES=5
UNANSWERED_POLL_INTERVAL_SECONDS=60
UNANSWERED_RENOTIFY_MINUTES=0
UNANSWERED_SUBSCRIBERS_ONLY=false
WATCHDOG_ENABLED=true
DB_PATH=/data/boltchatter.db
DRY_RUN=false
```

- [ ] **Step 3: Create `README.md`**

```markdown
# BoltChatter Welcome Bot

Headless Fanvue automation: welcomes new subscribers ~60s after Fanvue's own
auto-welcome, pings Telegram on every new sub, and alerts when a chat goes
unanswered for >=5 min. Works in agency and single-creator mode.

## Run locally
    python -m pip install -e ".[dev]"
    cp .env.example .env   # fill in tokens
    set -a && source .env && set +a
    DRY_RUN=true python -m boltchatter.app

## Test
    python -m pytest -v

## Deploy (Railway)
1. New service from this repo (Dockerfile auto-detected).
2. Attach a volume mounted at `/data` (persists the SQLite state).
3. Set env vars from `.env.example` (reuse the Papacito Telegram bot token + chat id).
4. First boot seeds existing subscribers into `seen` WITHOUT welcoming them
   (cold-start guard) — verify the logs say "bootstrap seeded N existing subscribers".
5. Point BoltChatter.com at the service (or reserve the domain for the future PWA).

## Notes
- No webhooks exist in the Fanvue API; detection is poll-based (well under the
  100 req/60s limit).
- The future PWA dashboard must reuse the Papacito install walkthrough (see spec).
- Telegram failures never block sends and vice versa.
```

- [ ] **Step 4: Verify the build (optional if Docker available)**

Run: `python -m pytest -v`
Expected: ALL PASS (deployment files don't affect tests)

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .env.example README.md
git commit -m "chore: Dockerfile, .env.example, README for Railway deploy"
```

---

## Task 22: Live smoke test (manual, DRY_RUN)

**Goal:** Confirm real API assumptions before going live: token validity, `sentAt`
granularity, the `not_answered` filter, and creator-scoped path parity (agency mode).

- [ ] **Step 1: Run read-only against the live API in dry-run**

```bash
set -a && source .env && set +a
DRY_RUN=true python -m boltchatter.app
```

- [ ] **Step 2: Verify in logs**
- "bootstrap seeded N existing subscribers" appears once.
- No `[DRY_RUN telegram]` welcome sends fire for existing subs (cold-start guard works).
- If you have a test chat sitting unanswered >5 min, a `[DRY_RUN telegram] ⏰ Unanswered…`
  line appears.

- [ ] **Step 3: One real end-to-end test**
- Set `DRY_RUN=false`, subscribe a throwaway test account to a creator.
- Confirm: Telegram new-sub ping fires; ~60s after the generic welcome, the custom
  welcome + photo lands in the chat.

- [ ] **Step 4: Record findings**
- Note actual `sentAt` granularity and any endpoint that 404s in agency mode in the spec's
  "Risks / notes" section, then commit that doc update if anything changed.

```bash
git add docs/superpowers/specs/2026-06-22-fanvue-welcome-bot-design.md
git commit -m "docs: record live smoke-test findings"
```
