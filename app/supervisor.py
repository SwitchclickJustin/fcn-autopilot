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

        # 2. If a supervisor LLM is configured, do a deeper check
        supervisor = await self._get_supervisor()
        if supervisor:
            username = persona.get("username", "User")
            tone = persona.get("default_tone", "casual")

            system = (
                "You are a safety supervisor for an adult chat room. "
                "Review the proposed message and the recent chat context.\n"
                f"Current persona tone: {tone}\n\n"
                "Respond with exactly one of:\n"
                "APPROVED - message is fine to send\n"
                "BLOCKED:<reason> - message would get the user kicked/banned\n"
                "MODIFY:<suggestion> - message needs changes, suggest what to say instead\n\n"
                "Check for: too fast, repetitive patterns, direct solicitation, "
                "aggressive language, spam behavior."
            )

            result = await supervisor.chat(
                system,
                f"Recent chat:\n\"\"\"\n{context}\n\"\"\"\n\nProposed message:\n\"{message}\""
            )

            if result:
                result = result.strip().upper()
                if result.startswith("APPROVED"):
                    return True, ""
                elif result.startswith("BLOCKED:"):
                    reason = result[8:].strip()
                    return False, f"Supervisor blocked: {reason}"
                elif result.startswith("MODIFY:"):
                    suggestion = result[7:].strip()
                    return False, f"Supervisor suggests: {suggestion}"

        # No supervisor or inconclusive — allow
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
                "You are a forensic analyst for chat accounts. "
                "A user account was just banned or kicked from a chat room. "
                "Analyze the last messages before the ban and the ban page content. "
                "Return JSON only:\n"
                "{\n"
                '  "likely_reason": "why they were banned",\n'
                '  "cooldown_adjustment": 0-300 (seconds to add), \n'
                '  "needs_proxy_rotation": true/false,\n'
                '  "needs_username_change": true/false,\n'
                '  "rule_name": "suggested rule name for this pattern",\n'
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
                        adjustments["cooldown_adjustment"] = parsed.get("cooldown_adjustment", 60)
                        adjustments["severity"] = parsed.get("severity", 5)
                        if parsed.get("needs_proxy_rotation", False):
                            adjustments["proxy_adjustment"] = "rotate"
                        adjustments["fingerprint_adjustment"] = {
                            "rotate_ua": parsed.get("needs_username_change", False)
                        }

                        # Create/update rule
                        rule_name = parsed.get("rule_name", f"learned_pattern_{int(time.time())}")
                        await db.upsert_rule({
                            "persona_id": persona_id,
                            "rule_name": rule_name,
                            "description": adjustments["likely_reason"],
                            "trigger_pattern": json.dumps({"keywords": [], "learned": True}),
                            "action": "slow_down",
                            "severity": adjustments["severity"],
                            "enabled": True,
                            "trigger_count": 1,
                            "last_triggered": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        })
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