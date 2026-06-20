"""PostgreSQL (Neon) database schema and async queries."""
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)

USE_NEON = bool(settings.neon_database_url)

if USE_NEON:
    import asyncpg
else:
    import aiosqlite

DB_PATH = settings.database_path

# ─── Schema ───
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS personas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT NOT NULL,
    gender TEXT DEFAULT 'm',
    bio TEXT DEFAULT '',
    goals TEXT DEFAULT '',
    telegram_handle TEXT DEFAULT '',
    default_tone TEXT DEFAULT 'casual',
    default_length TEXT DEFAULT 'medium',
    proxy_country TEXT DEFAULT 'us',
    proxy_custom TEXT DEFAULT '',
    user_agent TEXT DEFAULT 'random',
    timezone TEXT DEFAULT '',
    language TEXT DEFAULT '',
    fingerprint_rotation TEXT DEFAULT 'per_session',
    cooldown_min INTEGER DEFAULT 90,
    cooldown_max INTEGER DEFAULT 180,
    daily_cap INTEGER DEFAULT 150,
    selected_rooms TEXT DEFAULT '["SextChat"]',
    auto_reply_dms INTEGER DEFAULT 0,
    dm_gender_filter TEXT DEFAULT '[]',
    dm_blocklist TEXT DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    model TEXT DEFAULT 'gpt-4o-mini',
    api_key TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    temperature REAL DEFAULT 0.8,
    role TEXT DEFAULT 'chat',
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    persona_id TEXT REFERENCES personas(id),
    username TEXT,
    room_ids TEXT DEFAULT '["SextChat"]',
    status TEXT DEFAULT 'idle',
    auto_pilot INTEGER DEFAULT 0,
    browser_session_id TEXT DEFAULT '',
    browser_live_url TEXT DEFAULT '',
    messages_sent_today INTEGER DEFAULT 0,
    cooldown_until REAL DEFAULT 0,
    last_message_at TIMESTAMP DEFAULT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_log (
    id SERIAL PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    chat_type TEXT DEFAULT 'group',
    source TEXT DEFAULT 'ai',
    other_user TEXT DEFAULT '',
    message TEXT,
    tone_used TEXT DEFAULT '',
    supervisor_approved INTEGER DEFAULT 1,
    supervisor_note TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ban_events (
    id SERIAL PRIMARY KEY,
    session_id TEXT DEFAULT '',
    persona_id TEXT DEFAULT '',
    event_type TEXT,
    likely_reason TEXT DEFAULT '',
    context_before TEXT DEFAULT '[]',
    context_after TEXT DEFAULT '',
    cooldown_adjustment INTEGER DEFAULT 0,
    fingerprint_adjustment TEXT DEFAULT '{}',
    proxy_adjustment TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS supervisor_rules (
    id SERIAL PRIMARY KEY,
    persona_id TEXT DEFAULT '',
    rule_name TEXT UNIQUE,
    description TEXT DEFAULT '',
    trigger_pattern TEXT DEFAULT '{}',
    action TEXT DEFAULT 'warn',
    severity INTEGER DEFAULT 5,
    enabled INTEGER DEFAULT 1,
    trigger_count INTEGER DEFAULT 0,
    last_triggered TIMESTAMP DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_events (
    id SERIAL PRIMARY KEY,
    persona_id TEXT DEFAULT '',
    event_type TEXT,              -- message | handle_share | dm | conversion | ban
    room TEXT DEFAULT '',
    other_user TEXT DEFAULT '',
    content TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dm_conversations (
    id TEXT PRIMARY KEY,
    persona_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    other_user TEXT DEFAULT '',
    opener TEXT DEFAULT '',       -- bot's very first DM message (for conversion rate analysis)
    started_at TEXT,
    last_message_at TEXT,
    converted INTEGER DEFAULT 0,
    converted_at TEXT,
    bot_msg_count INTEGER DEFAULT 0,
    user_msg_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dm_messages (
    id TEXT PRIMARY KEY,
    conv_id TEXT,
    sender TEXT,                  -- 'bot' or 'user'
    content TEXT,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS persona_photos (
    id TEXT PRIMARY KEY,
    persona_id TEXT REFERENCES personas(id) ON DELETE CASCADE,
    filename TEXT DEFAULT '',
    url TEXT DEFAULT '',   -- Bunny.net CDN URL
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

async def get_db():
    """Get database connection (Neon PostgreSQL or SQLite fallback)."""
    if USE_NEON:
        conn = await asyncpg.connect(settings.neon_database_url)
        await conn.execute(SCHEMA_SQL)
        # persona_photos migration: add url column if the table was created with the old schema
        try:
            await conn.execute("ALTER TABLE persona_photos ADD COLUMN IF NOT EXISTS url TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE persona_photos DROP COLUMN IF EXISTS data")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE persona_photos DROP COLUMN IF EXISTS mime_type")
        except Exception:
            pass
        return conn
    else:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        return db

async def close_db(db):
    """Close database connection (works for both asyncpg and aiosqlite).

    asyncpg exposes is_closed(); aiosqlite does not — so only guard on the
    Neon path and let SQLite close unconditionally (it is opened per-query).
    """
    if not db:
        return
    try:
        if USE_NEON:
            if not db.is_closed():
                await db.close()
        else:
            await db.close()
    except Exception:
        pass

async def _fetchall(db, query, params=None):
    """Execute query and return list of dicts."""
    if USE_NEON:
        rows = await db.fetch(query, *(params or []))
        return [dict(r) for r in rows]
    else:
        if params:
            rows = await db.execute_fetchall(query, params)
        else:
            rows = await db.execute_fetchall(query)
        return [dict(r) for r in rows]

async def _execute(db, query, params=None):
    """Execute write query and commit.

    Coerce Python bools to ints: every boolean-ish column in this schema is
    declared INTEGER, and asyncpg (Neon) rejects a bool for an INTEGER column
    (sqlite is indifferent). This is the single write choke point, so doing it
    here fixes every INSERT/UPDATE (sessions.auto_pilot, personas.auto_reply_dms,
    llm_providers.enabled, ...).

    Also stringify UUIDs: the Browser Use SDK returns UUID objects for ids
    (browser_id, profile_id) and asyncpg rejects a UUID for a TEXT column.
    """
    def _coerce(p):
        if isinstance(p, bool):
            return int(p)
        if isinstance(p, uuid.UUID):
            return str(p)
        return p
    if params:
        params = [_coerce(p) for p in params]
    if USE_NEON:
        await db.execute(query, *(params or []))
    else:
        if params:
            await db.execute(query, params)
        else:
            await db.execute(query)
        await db.commit()

# ─── Persona CRUD ───
async def get_personas():
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM personas ORDER BY name")
    await close_db(db)
    return rows

async def get_persona(persona_id: str):
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM personas WHERE id = $1" if USE_NEON else "SELECT * FROM personas WHERE id = ?", [persona_id])
    await close_db(db)
    return rows[0] if rows else None

async def create_persona(data: dict):
    for field in ["selected_rooms", "dm_gender_filter", "dm_blocklist"]:
        if isinstance(data.get(field), list):
            data[field] = json.dumps(data[field])
    cols = ", ".join(data.keys())
    placeholders = ", ".join([f"${i+1}" for i in range(len(data))]) if USE_NEON else ", ".join(["?"] * len(data))
    db = await get_db()
    try:
        await _execute(db, f"INSERT INTO personas ({cols}) VALUES ({placeholders})", list(data.values()))
    except Exception as e:
        logger.error(f"PERSONA INSERT ERROR: {e}")
        logger.error(f"Columns: {cols}")
        logger.error(f"Values types: {[type(v).__name__ for v in data.values()]}")
        raise
    finally:
        await close_db(db)
    return data

async def update_persona(persona_id: str, data: dict):
    for field in ["selected_rooms", "dm_gender_filter", "dm_blocklist"]:
        if isinstance(data.get(field), list):
            data[field] = json.dumps(data[field])
    data["updated_at"] = datetime.utcnow()
    sets = ", ".join(f"{k} = ${i+1}" if USE_NEON else f"{k} = ?" for i, k in enumerate(data))
    vals = list(data.values()) + [persona_id]
    db = await get_db()
    where = "WHERE id = $"+str(len(data)+1) if USE_NEON else "WHERE id = ?"
    try:
        await _execute(db, f"UPDATE personas SET {sets} {where}", vals)
    except Exception as e:
        logger.error(f"UPDATE PERSONA ERROR: {e}")
        logger.error(f"Values types: {[type(v).__name__ for v in vals]}")
        raise
    finally:
        await close_db(db)

async def delete_persona(persona_id: str):
    db = await get_db()
    await _execute(db, "DELETE FROM personas WHERE id = $1" if USE_NEON else "DELETE FROM personas WHERE id = ?", [persona_id])
    await close_db(db)

# ─── LLM Provider CRUD ───
async def get_providers():
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM llm_providers ORDER BY priority")
    await close_db(db)
    return rows

async def create_provider(data: dict):
    cols = ", ".join(data.keys())
    placeholders = ", ".join([f"${i+1}" for i in range(len(data))]) if USE_NEON else ", ".join(["?"] * len(data))
    db = await get_db()
    await _execute(db, f"INSERT INTO llm_providers ({cols}) VALUES ({placeholders})", list(data.values()))
    await close_db(db)
    return data

async def delete_provider(provider_id: str):
    db = await get_db()
    await _execute(db, "DELETE FROM llm_providers WHERE id = $1" if USE_NEON else "DELETE FROM llm_providers WHERE id = ?", [provider_id])
    await close_db(db)

# ─── Session CRUD ───
async def create_session(data: dict):
    if isinstance(data.get("room_ids"), list):
        data["room_ids"] = json.dumps(data["room_ids"])
    cols = ", ".join(data.keys())
    placeholders = ", ".join([f"${i+1}" for i in range(len(data))]) if USE_NEON else ", ".join(["?"] * len(data))
    db = await get_db()
    await _execute(db, f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", list(data.values()))
    await close_db(db)
    return data

async def update_session(session_id: str, data: dict):
    if isinstance(data.get("room_ids"), list):
        data["room_ids"] = json.dumps(data["room_ids"])
    sets = ", ".join(f"{k} = ${i+1}" if USE_NEON else f"{k} = ?" for i, k in enumerate(data))
    vals = list(data.values()) + [session_id]
    db = await get_db()
    where = "WHERE id = $"+str(len(data)+1) if USE_NEON else "WHERE id = ?"
    await _execute(db, f"UPDATE sessions SET {sets} {where}", vals)
    await close_db(db)

async def get_session(session_id: str):
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM sessions WHERE id = $1" if USE_NEON else "SELECT * FROM sessions WHERE id = ?", [session_id])
    await close_db(db)
    return rows[0] if rows else None

async def get_active_session():
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM sessions WHERE status IN ('active','idle') ORDER BY last_seen_at DESC LIMIT 1")
    await close_db(db)
    return rows[0] if rows else None

# ─── Chat Log ───
async def log_chat(entry: dict):
    cols = ", ".join(entry.keys())
    placeholders = ", ".join([f"${i+1}" for i in range(len(entry))]) if USE_NEON else ", ".join(["?"] * len(entry))
    db = await get_db()
    await _execute(db, f"INSERT INTO chat_log ({cols}) VALUES ({placeholders})", list(entry.values()))
    await close_db(db)

async def get_chat_log(session_id: str, limit=50):
    db = await get_db()
    query = "SELECT * FROM chat_log WHERE session_id = $1 ORDER BY id DESC LIMIT $2" if USE_NEON else "SELECT * FROM chat_log WHERE session_id = ? ORDER BY id DESC LIMIT ?"
    rows = await _fetchall(db, query, [session_id, limit])
    await close_db(db)
    return rows

# ─── Bot Events / Stats ───
async def log_event(persona_id: str, event_type: str, room: str = "",
                    other_user: str = "", content: str = ""):
    """Record a bot action for stats (message/handle_share/dm/conversion/ban)."""
    entry = {"persona_id": persona_id or "", "event_type": event_type,
             "room": room or "", "other_user": other_user or "", "content": (content or "")[:500]}
    cols = ", ".join(entry.keys())
    placeholders = ", ".join([f"${i+1}" for i in range(len(entry))]) if USE_NEON else ", ".join(["?"] * len(entry))
    db = await get_db()
    try:
        await _execute(db, f"INSERT INTO bot_events ({cols}) VALUES ({placeholders})", list(entry.values()))
    except Exception as e:
        logger.error(f"log_event failed: {e}")
    finally:
        await close_db(db)

async def get_stats(start: str, end: str, persona_id: str = "") -> dict:
    """Counts by event_type in [start, end) (UTC 'YYYY-MM-DD HH:MM:SS' strings).
    Returns {messages, handle_shares, dms, conversions, bans}."""
    # asyncpg needs datetime objects for a timestamp column; sqlite compares text.
    if USE_NEON:
        s = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        e = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    else:
        s, e = start, end
    db = await get_db()
    if USE_NEON:
        q = "SELECT event_type, COUNT(*) AS c FROM bot_events WHERE created_at >= $1 AND created_at < $2"
        params = [s, e]
        if persona_id:
            q += " AND persona_id = $3"; params.append(persona_id)
        q += " GROUP BY event_type"
    else:
        q = "SELECT event_type, COUNT(*) AS c FROM bot_events WHERE created_at >= ? AND created_at < ?"
        params = [s, e]
        if persona_id:
            q += " AND persona_id = ?"; params.append(persona_id)
        q += " GROUP BY event_type"
    rows = await _fetchall(db, q, params)
    # distinct DM conversations started in range. started_at is TEXT written via
    # datetime.isoformat() → "YYYY-MM-DDTHH:MM:SS.ffffff" (note the 'T'). The incoming
    # bounds are space-separated ("YYYY-MM-DD HH:MM:SS"), and ' ' (0x20) sorts BEFORE
    # 'T' (0x54) — so a space-form upper bound silently excludes EVERY row and the
    # count is always 0. Match the stored ISO form by swapping the separator to 'T'.
    start_iso = start.replace(" ", "T")
    end_iso = end.replace(" ", "T")
    try:
        dq = ("SELECT COUNT(DISTINCT other_user) AS c FROM dm_conversations "
              "WHERE started_at >= $1 AND started_at < $2 AND other_user <> ''"
              if USE_NEON else
              "SELECT COUNT(DISTINCT other_user) AS c FROM dm_conversations "
              "WHERE started_at >= ? AND started_at < ? AND other_user <> ''")
        drows = await _fetchall(db, dq, [start_iso, end_iso])
    except Exception as e:
        logger.error(f"dm_conversations stats query failed: {e}")
        drows = []
    await close_db(db)
    counts = {r["event_type"]: r["c"] for r in rows}
    handle_shares = counts.get("handle_share", 0)
    photo_sends = counts.get("photo_sent", 0)
    conversions = counts.get("telegram_conversion", 0)
    # A photo carries the handle in the image, so every photo send is also a handle exposure.
    # Conversion rate = conversions / total handle exposures (text shares + photo sends).
    handle_exposures = handle_shares + photo_sends
    return {
        "messages": counts.get("message", 0),
        "handle_shares": handle_shares,
        "photo_sends": photo_sends,
        "handle_exposures": handle_exposures,
        "dms": (drows[0]["c"] if drows else 0),
        "conversions": conversions,
        "conversion_rate": round(100 * conversions / handle_exposures, 2) if handle_exposures else 0.0,
        "bans": counts.get("ban", 0),
    }

async def count_events_since(event_type: str, since: datetime) -> int:
    """Count bot_events of a type created at/after `since` (UTC datetime). Used for
    rolling-window + session conversion rates."""
    db = await get_db()
    try:
        if USE_NEON:
            rows = await _fetchall(
                db, "SELECT COUNT(*) AS c FROM bot_events WHERE event_type = $1 AND created_at >= $2",
                [event_type, since])
        else:
            rows = await _fetchall(
                db, "SELECT COUNT(*) AS c FROM bot_events WHERE event_type = ? AND created_at >= ?",
                [event_type, since.strftime("%Y-%m-%d %H:%M:%S")])
    finally:
        await close_db(db)
    return (rows[0]["c"] if rows else 0)


async def get_recent_events(limit=40):
    db = await get_db()
    q = "SELECT * FROM bot_events ORDER BY id DESC LIMIT $1" if USE_NEON else "SELECT * FROM bot_events ORDER BY id DESC LIMIT ?"
    rows = await _fetchall(db, q, [limit])
    await close_db(db)
    return rows

# ─── Ban Events ───
async def log_ban_event(entry: dict):
    cols = ", ".join(entry.keys())
    placeholders = ", ".join([f"${i+1}" for i in range(len(entry))]) if USE_NEON else ", ".join(["?"] * len(entry))
    db = await get_db()
    await _execute(db, f"INSERT INTO ban_events ({cols}) VALUES ({placeholders})", list(entry.values()))
    await close_db(db)

async def get_ban_events(limit=20):
    db = await get_db()
    query = "SELECT * FROM ban_events ORDER BY id DESC LIMIT $1" if USE_NEON else "SELECT * FROM ban_events ORDER BY id DESC LIMIT ?"
    rows = await _fetchall(db, query, [limit])
    await close_db(db)
    return rows

# ─── Supervisor Rules ───
async def get_rules():
    db = await get_db()
    rows = await _fetchall(db, "SELECT * FROM supervisor_rules ORDER BY severity DESC")
    await close_db(db)
    return rows

async def upsert_rule(data: dict):
    db = await get_db()
    if USE_NEON:
        existing = await db.fetch("SELECT id FROM supervisor_rules WHERE rule_name = $1", data["rule_name"])
    else:
        existing = await db.execute_fetchall("SELECT id FROM supervisor_rules WHERE rule_name = ?", (data["rule_name"],))
    
    if existing:
        sets = ", ".join(f"{k} = ${i+1}" if USE_NEON else f"{k} = ?" for i, k in enumerate(data))
        vals = list(data.values()) + [data["rule_name"]]
        where = "WHERE rule_name = $"+str(len(data)+1) if USE_NEON else "WHERE rule_name = ?"
        await _execute(db, f"UPDATE supervisor_rules SET {sets} {where}", vals)
    else:
        cols = ", ".join(data.keys())
        placeholders = ", ".join([f"${i+1}" for i in range(len(data))]) if USE_NEON else ", ".join(["?"] * len(data))
        await _execute(db, f"INSERT INTO supervisor_rules ({cols}) VALUES ({placeholders})", list(data.values()))
    await close_db(db)


# ─── DM Conversations ──────────────────────────────────────────────────────────

def _p(n: int) -> str:
    """Positional placeholder: $n for Neon, ? for SQLite."""
    return f"${n}" if USE_NEON else "?"


async def get_or_create_dm_conversation(persona_id: str, agent_id: str,
                                        other_user: str) -> str:
    """Return the id of the open DM conversation with other_user, creating one if needed."""
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        q = (f"SELECT id FROM dm_conversations WHERE persona_id = {_p(1)} "
             f"AND agent_id = {_p(2)} AND other_user = {_p(3)} "
             f"AND converted = 0 ORDER BY started_at DESC LIMIT 1")
        rows = await _fetchall(db, q, [persona_id, agent_id, other_user])
        if rows:
            return rows[0]["id"]
        conv_id = str(uuid.uuid4())
        await _execute(db,
            f"INSERT INTO dm_conversations (id, persona_id, agent_id, other_user, started_at, last_message_at) "
            f"VALUES ({_p(1)},{_p(2)},{_p(3)},{_p(4)},{_p(5)},{_p(6)})",
            [conv_id, persona_id or "", agent_id or "", other_user, now, now])
        return conv_id
    finally:
        await close_db(db)


async def log_dm_message(conv_id: str, sender: str, content: str,
                          is_opener: bool = False) -> None:
    """Append one message to a DM conversation and update counters + opener."""
    now = datetime.utcnow().isoformat()
    msg_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await _execute(db,
            f"INSERT INTO dm_messages (id, conv_id, sender, content, ts) "
            f"VALUES ({_p(1)},{_p(2)},{_p(3)},{_p(4)},{_p(5)})",
            [msg_id, conv_id, sender, (content or "")[:1000], now])
        # Increment the right counter and update last_message_at
        col = "bot_msg_count" if sender == "bot" else "user_msg_count"
        await _execute(db,
            f"UPDATE dm_conversations SET {col} = {col} + 1, last_message_at = {_p(1)} "
            f"WHERE id = {_p(2)}",
            [now, conv_id])
        # Store the bot's first message as the opener
        if is_opener:
            await _execute(db,
                f"UPDATE dm_conversations SET opener = {_p(1)} WHERE id = {_p(2)} AND opener = ''",
                [(content or "")[:500], conv_id])
    except Exception as e:
        logger.error(f"log_dm_message failed: {e}")
    finally:
        await close_db(db)


async def mark_dm_converted(conv_id: str) -> None:
    """Flag a DM conversation as converted (user confirmed they found us)."""
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        await _execute(db,
            f"UPDATE dm_conversations SET converted = 1, converted_at = {_p(1)} WHERE id = {_p(2)}",
            [now, conv_id])
    finally:
        await close_db(db)


async def get_top_converting_openers(persona_id: str = "", limit: int = 8) -> list:
    """Return bot opener messages from conversations that converted.

    Used to inject proven openers into the DM system prompt so the LLM
    can learn from what actually works.
    """
    db = await get_db()
    try:
        base = ("SELECT opener, COUNT(*) AS uses, SUM(converted) AS conversions "
                "FROM dm_conversations WHERE opener <> '' AND bot_msg_count > 0")
        params = []
        if persona_id:
            base += f" AND persona_id = {_p(1)}"
            params.append(persona_id)
        base += " GROUP BY opener ORDER BY conversions DESC, uses DESC LIMIT " + (
            _p(len(params) + 1) if USE_NEON else "?")
        params.append(limit)
        rows = await _fetchall(db, base, params)
        return [{"opener": r["opener"], "uses": r["uses"],
                 "conversions": r["conversions"],
                 "rate": round(r["conversions"] / r["uses"] * 100) if r["uses"] else 0}
                for r in rows]
    finally:
        await close_db(db)


async def get_dm_conversations(persona_id: str = "", limit: int = 100,
                                converted_only: bool = False) -> list:
    """Paginated DM conversation list for the history view."""
    db = await get_db()
    try:
        q = "SELECT * FROM dm_conversations WHERE 1=1"
        params = []
        if persona_id:
            params.append(persona_id)
            q += f" AND persona_id = {_p(len(params))}"
        if converted_only:
            q += " AND converted = 1"
        q += " ORDER BY last_message_at DESC LIMIT " + (_p(len(params) + 1) if USE_NEON else "?")
        params.append(limit)
        return await _fetchall(db, q, params)
    finally:
        await close_db(db)


async def get_dm_thread(conv_id: str) -> list:
    """Full message thread for one DM conversation."""
    db = await get_db()
    try:
        q = f"SELECT * FROM dm_messages WHERE conv_id = {_p(1)} ORDER BY ts ASC"
        return await _fetchall(db, q, [conv_id])
    finally:
        await close_db(db)


# ─── Agent Messages (merged group + DM feed) ──────────────────────────────────

async def get_agent_messages(limit: int = 200, persona_id: str = "") -> list:
    """Return merged feed of agent-sent messages: group (bot_events event_type='message')
    and DM (dm_messages sender='bot' with conv metadata), sorted by created_at DESC."""
    db = await get_db()
    try:
        # Group room messages from bot_events
        if USE_NEON:
            gq = ("SELECT created_at, persona_id, room, '' AS other_user, content, 'group' AS msg_type "
                  "FROM bot_events WHERE event_type = 'message'")
            gparams: list = []
            if persona_id:
                gq += f" AND persona_id = {_p(1)}"
                gparams.append(persona_id)
        else:
            gq = ("SELECT created_at, persona_id, room, '' AS other_user, content, 'group' AS msg_type "
                  "FROM bot_events WHERE event_type = 'message'")
            gparams = []
            if persona_id:
                gq += " AND persona_id = ?"
                gparams.append(persona_id)

        # DM messages from dm_messages JOIN dm_conversations (bot side only)
        if USE_NEON:
            dq = ("SELECT m.ts AS created_at, c.persona_id, '' AS room, c.other_user, m.content, 'dm' AS msg_type "
                  "FROM dm_messages m JOIN dm_conversations c ON m.conv_id = c.id "
                  "WHERE m.sender = 'bot'")
            dparams: list = []
            if persona_id:
                dq += f" AND c.persona_id = {_p(1)}"
                dparams.append(persona_id)
        else:
            dq = ("SELECT m.ts AS created_at, c.persona_id, '' AS room, c.other_user, m.content, 'dm' AS msg_type "
                  "FROM dm_messages m JOIN dm_conversations c ON m.conv_id = c.id "
                  "WHERE m.sender = 'bot'")
            dparams = []
            if persona_id:
                dq += " AND c.persona_id = ?"
                dparams.append(persona_id)

        group_rows = await _fetchall(db, gq, gparams)
        dm_rows = await _fetchall(db, dq, dparams)

        # Normalise created_at to string for consistent sorting
        def _ts(row: dict) -> str:
            v = row.get("created_at", "")
            if hasattr(v, "isoformat"):
                return v.isoformat()
            return str(v) if v else ""

        combined = []
        for r in group_rows:
            combined.append({
                "created_at": _ts(r),
                "persona_id": r.get("persona_id", ""),
                "room": r.get("room", ""),
                "other_user": "",
                "content": r.get("content", ""),
                "msg_type": "group",
            })
        for r in dm_rows:
            combined.append({
                "created_at": _ts(r),
                "persona_id": r.get("persona_id", ""),
                "room": "",
                "other_user": r.get("other_user", ""),
                "content": r.get("content", ""),
                "msg_type": "dm",
            })

        combined.sort(key=lambda x: x["created_at"], reverse=True)
        return combined[:limit]
    finally:
        await close_db(db)


# ─── Persona Photos ────────────────────────────────────────────────────────────

async def add_persona_photo(persona_id: str, filename: str, url: str) -> str:
    """Store a Bunny.net CDN URL photo for a persona. Returns the new photo id."""
    photo_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await _execute(db,
            f"INSERT INTO persona_photos (id, persona_id, filename, url) "
            f"VALUES ({_p(1)},{_p(2)},{_p(3)},{_p(4)})",
            [photo_id, persona_id, filename or "", url or ""])
    finally:
        await close_db(db)
    return photo_id


async def get_persona_photos(persona_id: str) -> list:
    """List photos for a persona."""
    db = await get_db()
    try:
        q = (f"SELECT id, persona_id, filename, url, created_at "
             f"FROM persona_photos WHERE persona_id = {_p(1)} ORDER BY created_at DESC")
        return await _fetchall(db, q, [persona_id])
    finally:
        await close_db(db)


async def get_persona_photo(photo_id: str) -> Optional[dict]:
    """Fetch one photo row (includes url)."""
    db = await get_db()
    try:
        q = f"SELECT * FROM persona_photos WHERE id = {_p(1)}"
        rows = await _fetchall(db, q, [photo_id])
        return rows[0] if rows else None
    finally:
        await close_db(db)


async def delete_persona_photo(photo_id: str) -> None:
    """Delete a persona photo by id."""
    db = await get_db()
    try:
        await _execute(db,
            f"DELETE FROM persona_photos WHERE id = {_p(1)}",
            [photo_id])
    finally:
        await close_db(db)