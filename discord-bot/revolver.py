"""
Revolver — cascading LLM provider with automatic failover.

Priority order:
  1. Gemini via GEMINI_KEY_1
  2. Gemini via GEMINI_KEY_2
  3. Groq  via GROQ_KEY  (llama-3.3-70b-versatile)

After every successful call, `last_used_provider` is updated so other parts
of the bot (e.g. zero status) can report which provider is currently active.
"""

import asyncio
import logging
import os

from google import genai
from google.genai import types as genai_types
from groq import Groq

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.0-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"

_RATE_LIMIT_SIGNALS = (
    "429",
    "quota",
    "rate_limit",
    "resource_exhausted",
    "too many requests",
)


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(signal in msg for signal in _RATE_LIMIT_SIGNALS)


async def _call_gemini(api_key: str, prompt: str, system_prompt: str | None) -> str:
    """Call Gemini asynchronously using the google-genai SDK."""

    def _sync() -> str:
        client = genai.Client(api_key=api_key)
        config = genai_types.GenerateContentConfig(
            system_instruction=system_prompt or "You are Zero, a helpful Discord bot.",
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        return response.text

    return await asyncio.to_thread(_sync)


async def _call_groq(api_key: str, prompt: str, system_prompt: str | None) -> str:
    """Call Groq asynchronously."""

    def _sync() -> str:
        client = Groq(api_key=api_key)
        messages = [
            {
                "role": "system",
                "content": system_prompt or "You are Zero, a helpful Discord bot.",
            },
            {"role": "user", "content": prompt},
        ]
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
        )
        return completion.choices[0].message.content

    return await asyncio.to_thread(_sync)


class Revolver:
    """
    Cascading LLM caller. Instantiate once and call `.generate()` freely.

    Usage:
        revolver = Revolver()
        reply = await revolver.generate("What is 2 + 2?")

    After every successful call `last_used_provider` holds the display name of
    the provider that responded (e.g. "Gemini (Key 1)"), useful for status commands.
    """

    def __init__(self) -> None:
        self.gemini_key_1: str | None = os.environ.get("GEMINI_KEY_1")
        self.gemini_key_2: str | None = os.environ.get("GEMINI_KEY_2")
        self.groq_key: str | None = os.environ.get("GROQ_KEY")
        self.last_used_provider: str = "Unknown"

    async def generate(self, prompt: str, system_prompt: str | None = None) -> str:
        """
        Send *prompt* to the first available provider.
        Falls through to the next provider only on rate-limit / quota errors.
        Other errors propagate immediately.
        Updates `last_used_provider` on each successful call.
        """
        # --- Attempt 1: Gemini key 1 ---
        if self.gemini_key_1:
            try:
                logger.debug("Revolver: trying GEMINI_KEY_1")
                result = await _call_gemini(self.gemini_key_1, prompt, system_prompt)
                self.last_used_provider = "Gemini (Key 1)"
                return result
            except Exception as exc:
                if _is_rate_limit(exc):
                    logger.warning("Revolver: GEMINI_KEY_1 rate-limited → GEMINI_KEY_2")
                else:
                    logger.error("Revolver: GEMINI_KEY_1 failed (%s) → GEMINI_KEY_2", exc)

        # --- Attempt 2: Gemini key 2 ---
        if self.gemini_key_2:
            try:
                logger.debug("Revolver: trying GEMINI_KEY_2")
                result = await _call_gemini(self.gemini_key_2, prompt, system_prompt)
                self.last_used_provider = "Gemini (Key 2)"
                return result
            except Exception as exc:
                if _is_rate_limit(exc):
                    logger.warning("Revolver: GEMINI_KEY_2 rate-limited → Groq")
                else:
                    logger.error("Revolver: GEMINI_KEY_2 failed (%s) → Groq", exc)

        # --- Attempt 3: Groq ---
        if self.groq_key:
            try:
                logger.debug("Revolver: trying GROQ_KEY")
                result = await _call_groq(self.groq_key, prompt, system_prompt)
                self.last_used_provider = "Groq (llama-3.3-70b)"
                return result
            except Exception as exc:
                logger.error("Revolver: Groq also failed: %s", exc)
                raise RuntimeError(f"All Revolver providers exhausted. Last error: {exc}") from exc

        raise RuntimeError(
            "Revolver: No API keys configured. "
            "Set GEMINI_KEY_1, GEMINI_KEY_2, and/or GROQ_KEY."
        )
