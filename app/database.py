"""PostgreSQL (Neon) database schema and async queries."""
import json
import os
from datetime import datetime
from app.config import settings

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
    last_message_at TIMESTAMP DEFAULT '',
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
    last_triggered TIMESTAMP DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

async def get_db():
    """Get database connection (Neon PostgreSQL or SQLite fallback)."""
    if USE_NEON:
        conn = await asyncpg.connect(settings.neon_database_url)
        await conn.execute(SCHEMA_SQL)
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
    """Close database connection."""
    if db and not db.is_closed():
        await db.close()

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
    """Execute write query and commit."""
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
    await _execute(db, f"INSERT INTO personas ({cols}) VALUES ({placeholders})", list(data.values()))
    await close_db(db)
    return data

async def update_persona(persona_id: str, data: dict):
    for field in ["selected_rooms", "dm_gender_filter", "dm_blocklist"]:
        if isinstance(data.get(field), list):
            data[field] = json.dumps(data[field])
    data["updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k} = ${i+1}" if USE_NEON else f"{k} = ?" for i, k in enumerate(data))
    vals = list(data.values()) + [persona_id]
    db = await get_db()
    where = "WHERE id = $"+str(len(data)+1) if USE_NEON else "WHERE id = ?"
    await _execute(db, f"UPDATE personas SET {sets} {where}", vals)
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