"""Pydantic models / schemas for all entities."""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import uuid

def new_id() -> str:
    return uuid.uuid4().hex[:12]

# ─── Persona ───
class Persona(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    username: str
    gender: str = "m"
    bio: str = ""
    goals: str = ""
    telegram_handle: str = ""
    group_openers: str = ""
    default_tone: str = "casual"
    default_length: str = "medium"
    proxy_country: str = "us"
    proxy_custom: str = ""
    user_agent: str = "random"
    timezone: str = ""
    language: str = ""
    fingerprint_rotation: str = "per_session"
    cooldown_min: int = 60
    cooldown_max: int = 120
    daily_cap: int = 100
    selected_rooms: List[str] = ["SextChat"]
    auto_reply_dms: bool = False
    dm_gender_filter: List[str] = []
    dm_blocklist: List[str] = []
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class PersonaCreate(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    username: str
    platform: str = "fcn"          # fcn | chatavenue
    gender: str = "f"
    bio: str = ""
    goals: str = ""
    telegram_handle: str = ""
    group_openers: str = ""
    default_tone: str = "flirty"
    default_length: str = "short"
    proxy_country: str = "us"
    proxy_custom: str = ""
    user_agent: str = "random"
    timezone: str = ""
    language: str = ""
    fingerprint_rotation: str = "per_session"
    cooldown_min: int = 90
    cooldown_max: int = 180
    daily_cap: int = 150
    selected_rooms: List[str] = ["SextChat"]
    auto_reply_dms: bool = False
    dm_gender_filter: List[str] = []
    dm_blocklist: List[str] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    username: Optional[str] = None
    platform: Optional[str] = None
    gender: Optional[str] = None
    bio: Optional[str] = None
    goals: Optional[str] = None
    telegram_handle: Optional[str] = None
    group_openers: Optional[str] = None
    default_tone: Optional[str] = None
    default_length: Optional[str] = None
    proxy_country: Optional[str] = None
    proxy_custom: Optional[str] = None
    user_agent: Optional[str] = None
    timezone: Optional[str] = None
    language: Optional[str] = None
    fingerprint_rotation: Optional[str] = None
    cooldown_min: Optional[int] = None
    cooldown_max: Optional[int] = None
    daily_cap: Optional[int] = None
    selected_rooms: Optional[List[str]] = None
    auto_reply_dms: Optional[bool] = None
    dm_gender_filter: Optional[List[str]] = None
    dm_blocklist: Optional[List[str]] = None

# ─── LLM Provider ───
class LLMProvider(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    provider_type: str  # openrouter, openai, anthropic, custom
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.8
    role: str = "chat"  # chat, supervisor, fallback
    enabled: bool = True
    priority: int = 0
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class LLMProviderCreate(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    provider_type: str
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.8
    role: str = "chat"
    priority: int = 0

# ─── Session ───
class Session(BaseModel):
    id: str = Field(default_factory=new_id)
    persona_id: str
    username: str
    room_ids: List[str] = ["SextChat"]
    status: str = "idle"  # active, idle, banned, error, reconnecting
    auto_pilot: bool = False
    browser_session_id: str = ""
    browser_live_url: str = ""
    messages_sent_today: int = 0
    cooldown_until: float = 0  # timestamp
    last_message_at: str = ""
    started_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    last_seen_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# ─── Chat Log ───
class ChatLogEntry(BaseModel):
    id: int = 0
    session_id: str
    chat_type: str = "group"  # group, dm
    source: str = "ai"  # user, ai, system
    other_user: str = ""
    message: str
    tone_used: str = ""
    supervisor_approved: bool = True
    supervisor_note: str = ""
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# ─── Ban Event ───
class BanEvent(BaseModel):
    id: int = 0
    session_id: str = ""
    persona_id: str = ""
    event_type: str  # kicked, banned, warning, error
    likely_reason: str = ""
    context_before: str = "[]"
    context_after: str = ""
    cooldown_adjustment: int = 0
    fingerprint_adjustment: str = "{}"
    proxy_adjustment: str = ""
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# ─── Supervisor Rule ───
class SupervisorRule(BaseModel):
    id: int = 0
    persona_id: str = ""
    rule_name: str
    description: str = ""
    trigger_pattern: str = "{}"
    action: str = "warn"  # block, warn, modify, slow_down, rotate_identity
    severity: int = 5
    enabled: bool = True
    trigger_count: int = 0
    last_triggered: str = ""
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# ─── WebSocket Messages ───
class WSMessage(BaseModel):
    type: str  # chat_update, status, ban, suggestion, error
    data: dict = {}