"""SQLite database schema and async queries."""
import aiosqlite
import json
import os
from datetime import datetime
from app.config import settings

DB_PATH = settings.database_path

# ─── Schema ───
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS personas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT NOT NULL,
    gender TEXT DEFAULT 'm',
    bio TEXT DEFAULT '',
    default_tone TEXT DEFAULT 'casual',
    default_length TEXT DEFAULT 'medium',
    proxy_country TEXT DEFAULT 'us',
    proxy_custom TEXT DEFAULT '',
    user_agent TEXT DEFAULT 'random',
    timezone TEXT DEFAULT '',
    language TEXT DEFAULT '',
    fingerprint_rotation TEXT DEFAULT 'per_session',
    cooldown_min INTEGER DEFAULT 60,
    cooldown_max INTEGER DEFAULT 120,
    daily_cap INTEGER DEFAULT 100,
    selected_rooms TEXT DEFAULT '["SextChat"]',
    auto_reply_dms INTEGER DEFAULT 0,
    dm_gender_filter TEXT DEFAULT '[]',
    dm_blocklist TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
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
    created_at TEXT DEFAULT (datetime('now'))
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
    last_message_at TEXT DEFAULT '',
    started_at TEXT DEFAULT (datetime('now')),
    last_seen_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    chat_type TEXT DEFAULT 'group',
    source TEXT DEFAULT 'ai',
    other_user TEXT DEFAULT '',
    message TEXT,
    tone_used TEXT DEFAULT '',
    supervisor_approved INTEGER DEFAULT 1,
    supervisor_note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ban_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT DEFAULT '',
    persona_id TEXT DEFAULT '',
    event_type TEXT,
    likely_reason TEXT DEFAULT '',
    context_before TEXT DEFAULT '[]',
    context_after TEXT DEFAULT '',
    cooldown_adjustment INTEGER DEFAULT 0,
    fingerprint_adjustment TEXT DEFAULT '{}',
    proxy_adjustment TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supervisor_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT DEFAULT '',
    rule_name TEXT UNIQUE,
    description TEXT DEFAULT '',
    trigger_pattern TEXT DEFAULT '{}',
    action TEXT DEFAULT 'warn',
    severity INTEGER DEFAULT 5,
    enabled INTEGER DEFAULT 1,
    trigger_count INTEGER DEFAULT 0,
    last_triggered TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
"""

async def get_db():
    """Get aiosqlite connection (creates tables on first connection)."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db

# ─── Persona CRUD ───
async def get_personas():
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM personas ORDER BY name")
    await db.close()
    return [dict(r) for r in rows]

async def get_persona(persona_id: str):
    db = await get_db()
    row = await db.execute_fetchall("SELECT * FROM personas WHERE id = ?", (persona_id,))
    await db.close()
    return dict(row[0]) if row else None

async def create_persona(data: dict):
    data["selected_rooms"] = json.dumps(data.get("selected_rooms", ["SextChat"]))
    data["dm_gender_filter"] = json.dumps(data.get("dm_gender_filter", []))
    data["dm_blocklist"] = json.dumps(data.get("dm_blocklist", []))
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    db = await get_db()
    await db.execute(f"INSERT INTO personas ({cols}) VALUES ({placeholders})", list(data.values()))
    await db.commit()
    await db.close()
    return data

async def update_persona(persona_id: str, data: dict):
    if "selected_rooms" in data and isinstance(data["selected_rooms"], list):
        data["selected_rooms"] = json.dumps(data["selected_rooms"])
    if "dm_gender_filter" in data and isinstance(data["dm_gender_filter"], list):
        data["dm_gender_filter"] = json.dumps(data["dm_gender_filter"])
    if "dm_blocklist" in data and isinstance(data["dm_blocklist"], list):
        data["dm_blocklist"] = json.dumps(data["dm_blocklist"])
    data["updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k} = ?" for k in data)
    vals = list(data.values()) + [persona_id]
    db = await get_db()
    await db.execute(f"UPDATE personas SET {sets} WHERE id = ?", vals)
    await db.commit()
    await db.close()

async def delete_persona(persona_id: str):
    db = await get_db()
    await db.execute("DELETE FROM personas WHERE id = ?", (persona_id,))
    await db.commit()
    await db.close()

# ─── LLM Provider CRUD ───
async def get_providers():
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM llm_providers ORDER BY priority")
    await db.close()
    return [dict(r) for r in rows]

async def create_provider(data: dict):
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    db = await get_db()
    await db.execute(f"INSERT INTO llm_providers ({cols}) VALUES ({placeholders})", list(data.values()))
    await db.commit()
    await db.close()
    return data

async def delete_provider(provider_id: str):
    db = await get_db()
    await db.execute("DELETE FROM llm_providers WHERE id = ?", (provider_id,))
    await db.commit()
    await db.close()

# ─── Session CRUD ───
async def create_session(data: dict):
    data["room_ids"] = json.dumps(data.get("room_ids", ["SextChat"]))
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    db = await get_db()
    await db.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", list(data.values()))
    await db.commit()
    await db.close()
    return data

async def update_session(session_id: str, data: dict):
    if "room_ids" in data and isinstance(data["room_ids"], list):
        data["room_ids"] = json.dumps(data["room_ids"])
    sets = ", ".join(f"{k} = ?" for k in data)
    vals = list(data.values()) + [session_id]
    db = await get_db()
    await db.execute(f"UPDATE sessions SET {sets} WHERE id = ?", vals)
    await db.commit()
    await db.close()

async def get_session(session_id: str):
    db = await get_db()
    row = await db.execute_fetchall("SELECT * FROM sessions WHERE id = ?", (session_id,))
    await db.close()
    return dict(row[0]) if row else None

async def get_active_session():
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM sessions WHERE status IN ('active','idle') ORDER BY last_seen_at DESC LIMIT 1")
    await db.close()
    return dict(rows[0]) if rows else None

# ─── Chat Log ───
async def log_chat(entry: dict):
    cols = ", ".join(entry.keys())
    placeholders = ", ".join(["?"] * len(entry))
    db = await get_db()
    await db.execute(f"INSERT INTO chat_log ({cols}) VALUES ({placeholders})", list(entry.values()))
    await db.commit()
    await db.close()

async def get_chat_log(session_id: str, limit=50):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM chat_log WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit))
    await db.close()
    return [dict(r) for r in rows]

# ─── Ban Events ───
async def log_ban_event(entry: dict):
    cols = ", ".join(entry.keys())
    placeholders = ", ".join(["?"] * len(entry))
    db = await get_db()
    await db.execute(f"INSERT INTO ban_events ({cols}) VALUES ({placeholders})", list(entry.values()))
    await db.commit()
    await db.close()

async def get_ban_events(limit=20):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM ban_events ORDER BY id DESC LIMIT ?", (limit,))
    await db.close()
    return [dict(r) for r in rows]

# ─── Supervisor Rules ───
async def get_rules():
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM supervisor_rules ORDER BY severity DESC")
    await db.close()
    return [dict(r) for r in rows]

async def upsert_rule(data: dict):
    db = await get_db()
    existing = await db.execute_fetchall("SELECT id FROM supervisor_rules WHERE rule_name = ?", (data["rule_name"],))
    if existing:
        sets = ", ".join(f"{k} = ?" for k in data)
        vals = list(data.values()) + [data["rule_name"]]
        await db.execute(f"UPDATE supervisor_rules SET {sets} WHERE rule_name = ?", vals)
    else:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        await db.execute(f"INSERT INTO supervisor_rules ({cols}) VALUES ({placeholders})", list(data.values()))
    await db.commit()
    await db.close()