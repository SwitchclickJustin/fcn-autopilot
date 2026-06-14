"""Auto-pilot engine — reads chat, generates responses, sends messages, handles DMs."""
import asyncio
import json
import logging
import random
import time
from typing import Optional
from app.browser import browser_manager, BrowserSession
from app.providers import provider_registry, LLMClient
from app.supervisor import supervisor_engine
import app.database as db

logger = logging.getLogger(__name__)

class AutoPilotEngine:
    """Main auto-pilot loop. Runs as a background asyncio task."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._enabled: bool = False
        self._session_id: str = ""
        self._last_messages: list = []
        self._last_dm_check: float = 0
        self._messages_today: int = 0
        self._daily_cap: int = 100
        self._cooldown_until: float = 0
        self._cooldown_min: int = 60
        self._cooldown_max: int = 120
        self._persona: dict = {}
        self._chat_provider: Optional[LLMClient] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self, session_id: str, persona: dict):
        """Start the auto-pilot loop."""
        self._session_id = session_id
        self._persona = persona
        self._cooldown_min = persona.get("cooldown_min", 60)
        self._cooldown_max = persona.get("cooldown_max", 120)
        self._daily_cap = persona.get("daily_cap", 100)
        self._messages_today = 0
        self._chat_provider = provider_registry.get_chat_provider()

        if not self._chat_provider:
            logger.error("No chat provider configured — cannot start auto-pilot")
            return

        self._enabled = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Auto-pilot started")

    async def stop(self):
        """Stop the auto-pilot loop."""
        self._enabled = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Auto-pilot stopped")

    async def _run_loop(self):
        """Main auto-pilot loop."""
        while self._enabled:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto-pilot tick error: {e}")
            await asyncio.sleep(3)

    async def _tick(self):
        """Single iteration of the auto-pilot loop."""
        session = browser_manager.current_session
        if not session or session.status != "logged_in":
            return

        # 1. Read group chat
        messages = await session.read_chat()
        if not messages or messages == self._last_messages:
            return

        new_messages = messages[len(self._last_messages):] if self._last_messages else messages[-3:]
        self._last_messages = messages

        if not new_messages:
            return

        # 2. Check if we should respond (new relevant messages)
        for msg in new_messages:
            if not self._enabled:
                return
            if self._messages_today >= self._daily_cap:
                logger.info("Daily message cap reached — pausing")
                self._enabled = False
                return

            # Check cooldown
            if time.time() < self._cooldown_until:
                continue

            # Check if message is relevant (mentions username or is recent)
            username = self._persona.get("username", "")
            is_addressed = username.lower() in msg.lower() if username else False

            if not is_addressed:
                # In group chat, only respond if addressed or sometimes proactively
                if random.random() > 0.15:  # 15% chance to chime in
                    continue

            # 3. Generate response
            context = "\n".join(self._last_messages[-10:])
            response = await self._generate_response(context)
            if not response:
                continue

            # 4. Supervisor check
            approved, note = await supervisor_engine.pre_flight(response, context, self._persona)
            if not approved:
                logger.info(f"Supervisor blocked: {note}")
                await db.log_chat({
                    "session_id": self._session_id,
                    "chat_type": "group",
                    "source": "ai",
                    "message": response,
                    "supervisor_approved": False,
                    "supervisor_note": note
                })
                continue

            # 5. Send
            sent = await session.send_message(response)
            if sent:
                self._messages_today += 1
                cooldown = random.randint(self._cooldown_min, self._cooldown_max)
                self._cooldown_until = time.time() + cooldown

                await db.log_chat({
                    "session_id": self._session_id,
                    "chat_type": "group",
                    "source": "ai",
                    "message": response,
                    "tone_used": self._persona.get("default_tone", "casual"),
                    "supervisor_approved": True,
                    "supervisor_note": ""
                })

                await db.update_session(self._session_id, {
                    "messages_sent_today": self._messages_today,
                    "last_message_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                })
                logger.info(f"Sent: {response[:60]}")

    async def _generate_response(self, context: str) -> Optional[str]:
        """Generate a chat response using the configured LLM."""
        provider = self._chat_provider
        if not provider:
            return None

        persona = self._persona
        tone = persona.get("default_tone", "casual")
        length = persona.get("default_length", "medium")
        bio = persona.get("bio", "")
        username = persona.get("username", "")

        system = (
            f"You are chatting in an adult chat room (18+). Your username is {username}.\n"
            f"Tone: {tone}. Message length: {length}.\n"
            f"Personality: {bio}\n\n"
            f"Rules: Be natural and conversational. Vary your responses. "
            f"Don't overuse emoji. Match the room's vibe. "
            f"Never include your username prefix. Just send the message."
        )

        user = f"Recent chat:\n\"\"\"\n{context}\n\"\"\"\n\nRespond naturally."

        return await provider.chat(system, user)

    async def generate_suggestions(self, context: str, count: int = 5) -> list:
        """Generate response suggestions (for manual mode)."""
        provider = self._chat_provider
        if not provider:
            return ["⚠️ No LLM provider configured"]

        persona = self._persona
        tone = persona.get("default_tone", "casual")
        bio = persona.get("bio", "")

        system = (
            f"Suggest {count} different realistic chat responses. "
            f"Tone: {tone}. Personality: {bio}\n"
            f"Number them 1-{count}. No explanations. Just the suggestions."
        )

        result = await provider.chat(system, f"Chat context:\n\"\"\"\n{context}\n\"\"\"\n\nSuggestions:")
        if not result:
            return ["⚠️ Failed to generate"]

        suggestions = []
        for line in result.split("\n"):
            line = line.strip()
            if not line:
                continue
            import re
            m = re.match(r'^\d+[.)]\s*(.*)', line)
            if m:
                suggestions.append(m.group(1))
            elif not suggestions:
                suggestions.append(line)

        return suggestions[:count] if suggestions else [result.strip()]

auto_pilot = AutoPilotEngine()