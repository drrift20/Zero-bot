"""
Revolver — cascading LLM provider with automatic failover.

Priority order:
  1. Gemini via GEMINI_KEY_1
  2. Gemini via GEMINI_KEY_2
  3. Groq  via GROQ_KEY  (llama-3.3-70b-versatile)
"""

import asyncio
import logging
import os

import google.generativeai as genai
from groq import Groq

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-1.5-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"

# HTTP status codes / exception substrings that indicate a rate limit
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
    """Call Gemini synchronously in a thread so the event loop stays free."""

    def _sync():
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system_prompt or "You are Zero, a helpful Discord bot.",
        )
        response = model.generate_content(prompt)
        return response.text

    return await asyncio.to_thread(_sync)


async def _call_groq(api_key: str, prompt: str, system_prompt: str | None) -> str:
    """Call Groq synchronously in a thread so the event loop stays free."""

    def _sync():
        client = Groq(api_key=api_key)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        else:
            messages.append({"role": "system", "content": "You are Zero, a helpful Discord bot."})
        messages.append({"role": "user", "content": prompt})

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
    """

    def __init__(self) -> None:
        self.gemini_key_1: str | None = os.environ.get("GEMINI_KEY_1")
        self.gemini_key_2: str | None = os.environ.get("GEMINI_KEY_2")
        self.groq_key: str | None = os.environ.get("GROQ_KEY")

    async def generate(self, prompt: str, system_prompt: str | None = None) -> str:
        """
        Send *prompt* to the first available provider.
        Falls through to the next provider only on rate-limit / quota errors.
        Other errors (bad key, network, etc.) propagate immediately.
        """
        # --- Attempt 1: Gemini key 1 ---
        if self.gemini_key_1:
            try:
                logger.debug("Revolver: trying GEMINI_KEY_1")
                return await _call_gemini(self.gemini_key_1, prompt, system_prompt)
            except Exception as exc:
                if _is_rate_limit(exc):
                    logger.warning("Revolver: GEMINI_KEY_1 rate-limited, switching to GEMINI_KEY_2")
                else:
                    logger.error("Revolver: GEMINI_KEY_1 failed (%s), switching to GEMINI_KEY_2", exc)

        # --- Attempt 2: Gemini key 2 ---
        if self.gemini_key_2:
            try:
                logger.debug("Revolver: trying GEMINI_KEY_2")
                return await _call_gemini(self.gemini_key_2, prompt, system_prompt)
            except Exception as exc:
                if _is_rate_limit(exc):
                    logger.warning("Revolver: GEMINI_KEY_2 rate-limited, falling back to Groq")
                else:
                    logger.error("Revolver: GEMINI_KEY_2 failed (%s), falling back to Groq", exc)

        # --- Attempt 3: Groq ---
        if self.groq_key:
            try:
                logger.debug("Revolver: trying GROQ_KEY")
                return await _call_groq(self.groq_key, prompt, system_prompt)
            except Exception as exc:
                logger.error("Revolver: Groq also failed: %s", exc)
                raise RuntimeError(f"All Revolver providers exhausted. Last error: {exc}") from exc

        raise RuntimeError(
            "Revolver: No API keys are configured. "
            "Set GEMINI_KEY_1, GEMINI_KEY_2, and/or GROQ_KEY."
        )
