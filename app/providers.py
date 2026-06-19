"""LLM Provider registry — supports OpenRouter, OpenAI, Anthropic, and custom endpoints."""
import json
import httpx
import logging
from typing import Optional, List
from app.config import settings

logger = logging.getLogger(__name__)

class LLMClient:
    """Generic LLM client that routes to the right provider."""
    
    def __init__(self, provider: dict):
        self.name = provider.get("name", "unnamed")
        self.provider_type = provider.get("provider_type", "openrouter")
        self.model = provider.get("model", "gpt-4o-mini")
        self.api_key = provider.get("api_key", "")
        self.base_url = provider.get("base_url", "").rstrip("/")
        self.temperature = provider.get("temperature", 0.8)
        self.role = provider.get("role", "chat")

    def _get_endpoint_and_key(self) -> tuple:
        """Return (api_url, api_key, model_name) based on provider type."""
        if self.provider_type == "openrouter":
            return (
                "https://openrouter.ai/api/v1/chat/completions",
                self.api_key or settings.openrouter_api_key,
                self.model
            )
        elif self.provider_type == "openai":
            return (
                "https://api.openai.com/v1/chat/completions",
                self.api_key or settings.openai_api_key,
                self.model
            )
        elif self.provider_type == "anthropic":
            return (
                "https://api.anthropic.com/v1/messages",
                self.api_key or settings.anthropic_api_key,
                self.model
            )
        else:  # custom
            url = self.base_url or "https://api.openai.com/v1"
            return (
                f"{url}/chat/completions",
                self.api_key,
                self.model
            )

    async def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> Optional[str]:
        """Send a chat completion request. Returns the response text or None."""
        url, key, model = self._get_endpoint_and_key()
        
        if not key:
            logger.error(f"No API key for provider {self.name} ({self.provider_type})")
            return None

        headers = {
            "Content-Type": "application/json",
        }

        if self.provider_type == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": self.temperature,
                "messages": [
                    {"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}
                ]
            }
        else:
            headers["Authorization"] = f"Bearer {key}"
            if self.provider_type == "openrouter":
                headers["HTTP-Referer"] = "https://fcn-autopilot.railway.app"
                headers["X-Title"] = "FCN Auto-Pilot"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                # min_p clamps the low-probability tail so high temps (RP models like Lunaris
                # want temp ~1.3) stay COHERENT instead of sampling garbage tokens/foreign
                # scripts/code. Without this, temp >1.0 on a small model produces gibberish.
                "min_p": 0.1,
                # Push the model away from formulaic, repeated phrasing so broadcasts vary.
                "frequency_penalty": 0.6,
                "presence_penalty": 0.5,
            }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                if self.provider_type == "anthropic":
                    return data.get("content", [{}])[0].get("text", "")
                else:
                    return data.get("choices", [{}])[0].get("message", {}).get("content", "")

        except httpx.HTTPStatusError as e:
            logger.error(f"LLM API error ({self.name}): {e.response.status_code} - {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"LLM request failed ({self.name}): {e}")
            return None


class ProviderRegistry:
    """Manages all configured LLM providers with failover support."""

    def __init__(self):
        self._providers: List[LLMClient] = []

    def load_from_db(self, providers_data: list):
        self._providers = []
        for p in sorted(providers_data, key=lambda x: x.get("priority", 0)):
            if p.get("enabled", True):
                self._providers.append(LLMClient(p))
        logger.info(f"Loaded {len(self._providers)} LLM providers")

    def get_chat_provider(self) -> Optional[LLMClient]:
        """Get the first enabled provider with role='chat'."""
        for p in self._providers:
            if p.role == "chat":
                return p
        return self._providers[0] if self._providers else None

    def get_supervisor_provider(self) -> Optional[LLMClient]:
        """Get the first enabled provider with role='supervisor'."""
        for p in self._providers:
            if p.role == "supervisor":
                return p
        # Fallback to chat provider
        return self.get_chat_provider()

    def get_fallback_provider(self) -> Optional[LLMClient]:
        """Get the first enabled provider with role='fallback'."""
        for p in self._providers:
            if p.role == "fallback":
                return p
        return None

provider_registry = ProviderRegistry()