"""Supervisor engine — pre-flight checks, post-mortem analysis, pattern learning."""
import json
import logging
import time
from typing import Optional, Tuple
from app.providers import provider_registry, LLMClient
import app.database as db

logger = logging.getLogger(__name__)

class SupervisorEngine:
    """Monitors behavior, learns from bans, adjusts auto-pilot settings."""

    def __init__(self):
        self._supervisor: Optional[LLMClient] = None
        self._rules_cache: list = []
        self._rules_loaded: float = 0

    async def _load_rules(self):
        """Load supervisor rules from DB (cache for 60s)."""
        if time.time() - self._rules_loaded > 60:
            self._rules_cache = await db.get_rules()
            self._rules_loaded = time.time()

    async def _get_supervisor(self) -> Optional[LLMClient]:
        """Get or cache the supervisor LLM provider."""
        if not self._supervisor:
            self._supervisor = provider_registry.get_supervisor_provider()
        return self._supervisor

    async def pre_flight(self, message: str, context: str, persona: dict) -> Tuple[bool, str]:
        """
        Check a message before sending.
        Returns (approved: bool, note: str).
        """
        await self._load_rules()

        # 1. Quick rule-based checks (no LLM call needed)
        for rule in self._rules_cache:
            if not rule.get("enabled", True):
                continue
            if rule["action"] == "block":
                # Check trigger pattern
                pattern = rule.get("trigger_pattern", "{}")
                try:
                    p = json.loads(pattern)
                except json.JSONDecodeError:
                    continue

                # Keyword-based rules
                keywords = p.get("keywords", [])
                if keywords:
                    msg_lower = message.lower()
                    for kw in keywords:
                        if kw.lower() in msg_lower:
                            await self._increment_rule(rule["id"])
                            return False, f"Blocked by rule '{rule['rule_name']}': keyword '{kw}'"

        # We intentionally do NOT run a generic "block all solicitation" LLM check —
        # the bot is *meant* to pitch. pre_flight only enforces phrasings the
        # supervisor LEARNED got us banned (the keyword rules above), so the bot
        # keeps pitching but stops repeating the exact lines that triggered bans.
        return True, ""

    async def analyze_ban(self, session_id: str, persona_id: str, context_before: list, context_after: str) -> dict:
        """
        After a ban, analyze what caused it and return adjustments.
        """
        supervisor = await self._get_supervisor()
        adjustments = {
            "likely_reason": "unknown",
            "cooldown_adjustment": 60,
            "proxy_adjustment": "",
            "fingerprint_adjustment": {},
            "severity": 5
        }

        if supervisor:
            system = (
                "You are a forensic analyst for a flirty chat bot that pitches its private "
                "contact handle. It just got banned/kicked. Identify the SPECIFIC short phrasings "
                "in its last messages that most likely triggered the moderation ban (e.g. overt "
                "solicitation like 'private pics', 'send you pics', 'add me on'). Do NOT list the "
                "contact handle itself or generic flirting — only the risky phrasings to STOP using. "
                "Return JSON only:\n"
                "{\n"
                '  "likely_reason": "why they were banned",\n'
                '  "avoid_phrases": ["short phrase 1", "short phrase 2"],\n'
                '  "rule_name": "short snake_case name for this pattern",\n'
                '  "severity": 1-10\n'
                "}"
            )

            result = await supervisor.chat(
                system,
                f"Messages before ban:\n\"\"\"\n{json.dumps(context_before[-10:])}\n\"\"\"\n\nBan page:\n\"\"\"\n{context_after}\n\"\"\""
            )

            if result:
                import re
                json_match = re.search(r'\{.*\}', result, re.DOTALL)
                if json_match:
                    try:
                        parsed = json.loads(json_match.group())
                        adjustments["likely_reason"] = parsed.get("likely_reason", "unknown")
                        adjustments["severity"] = parsed.get("severity", 5)
                        # Phrasings to STOP using → pre_flight blocks any message containing them
                        avoid = [p.strip() for p in parsed.get("avoid_phrases", []) if p and len(p.strip()) >= 3][:6]
                        adjustments["avoid_phrases"] = avoid
                        if avoid:
                            rule_name = parsed.get("rule_name") or f"banned_phrasing_{int(time.time())}"
                            await db.upsert_rule({
                                "persona_id": persona_id,
                                "rule_name": rule_name,
                                "description": adjustments["likely_reason"],
                                "trigger_pattern": json.dumps({"keywords": avoid, "learned": True}),
                                "action": "block",
                                "severity": adjustments["severity"],
                                "enabled": True,
                                "trigger_count": 1,
                                "last_triggered": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            })
                            logger.info(f"supervisor learned to avoid: {avoid}")
                    except json.JSONDecodeError:
                        pass

        # Log the ban event
        await db.log_ban_event({
            "session_id": session_id,
            "persona_id": persona_id,
            "event_type": "banned",
            "likely_reason": adjustments["likely_reason"],
            "context_before": json.dumps(context_before[-10:]),
            "context_after": context_after[:500],
            "cooldown_adjustment": adjustments["cooldown_adjustment"],
            "fingerprint_adjustment": json.dumps(adjustments["fingerprint_adjustment"]),
            "proxy_adjustment": adjustments["proxy_adjustment"]
        })

        return adjustments

    async def _increment_rule(self, rule_id: int):
        """Increment a rule's trigger count."""
        try:
            db_conn = await db.get_db()
            await db_conn.execute(
                "UPDATE supervisor_rules SET trigger_count = trigger_count + 1, last_triggered = ? WHERE id = ?",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rule_id)
            )
            await db_conn.commit()
            await db_conn.close()
        except Exception:
            pass

supervisor_engine = SupervisorEngine()