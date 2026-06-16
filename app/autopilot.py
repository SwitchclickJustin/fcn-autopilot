"""Auto-pilot engine — delegates browser work to BotOrchestrator.

The orchestrator (app.browser.browser_manager) handles:
  - SDK profile/session management
  - Browser provisioning with Decoda proxy
  - SDK agent login (autonomous navigation + form filling)
  - CDP fast loop (read_chat / send_message via JS evaluate)
  - 50 concurrent bot scaling

This module provides the LLM response generation and DB logging layer.
"""
import asyncio
import json
import logging
import random
import re
import time
from typing import Optional

from app.browser import browser_manager
from app.providers import provider_registry, LLMClient
from app.supervisor import supervisor_engine
import app.database as db

logger = logging.getLogger(__name__)


class AutoPilotEngine:
    """Auto-pilot — starts/stops bot sessions via BotOrchestrator.

    Delegates all browser work to browser_manager (BotOrchestrator).
    This module handles LLM response generation, supervisor checks,
    cooldowns, daily caps, and DB logging.
    """

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._enabled: bool = False
        self._session_id: str = ""
        self._last_messages: list = []
        self._messages_today: int = 0
        self._daily_cap: int = 100
        self._cooldown_until: float = 0
        self._cooldown_min: int = 60
        self._cooldown_max: int = 120
        self._persona: dict = {}
        self._chat_provider: Optional[LLMClient] = None
        self._current_username: str = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self, session_id: str, persona: dict):
        """Start a bot session: provision browser, log in, begin auto-pilot.

        Delegates to BotOrchestrator.start_bot() which handles:
          1. SDK profile (persistent cookies)
          2. Browser with Decoda proxy
          3. SDK agent login
          4. CDP connection
          5. Internal auto-pilot loop
        """
        self._session_id = session_id
        self._persona = persona
        self._current_username = persona.get("username", "ChatBot_42")
        self._cooldown_min = persona.get("cooldown_min", 60)
        self._cooldown_max = persona.get("cooldown_max", 120)
        self._daily_cap = persona.get("daily_cap", 100)
        self._messages_today = 0
        self._chat_provider = provider_registry.get_chat_provider()

        if not self._chat_provider:
            logger.error("No chat provider configured — cannot start auto-pilot")
            return

        # Start the browser session via orchestrator
        worker = await browser_manager.start_bot(persona)
        if not worker:
            logger.error("Failed to start browser session")
            return

        self._enabled = True
        logger.info(f"Auto-pilot started for {self._current_username}, "
                     f"browser: {worker.live_url[:60] if worker.live_url else 'none'}")

    async def stop(self):
        """Stop the bot session and persist its profile."""
        self._enabled = False
        if self._current_username:
            await browser_manager.stop_bot(self._current_username)
            logger.info(f"Auto-pilot stopped for {self._current_username}")

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
            m = re.match(r'^\d+[.)]\s*(.*)', line)
            if m:
                suggestions.append(m.group(1))
            elif not suggestions:
                suggestions.append(line)

        return suggestions[:count] if suggestions else [result.strip()]


auto_pilot = AutoPilotEngine()