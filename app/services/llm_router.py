"""CAM's reasoning layer.

Groq is the default for sub-second tool-calling. xAI Grok is selected for
architecturally heavy reasoning (multi-step planning, codebase reviews).

The router always returns a (spoken_reply, optional_command) tuple so the
session can fan-out TTS and execution in parallel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import span
from app.schemas.messages import Command

log = get_logger(__name__)


_SYSTEM_PROMPT = """You are CAM, the Conversational Architecture Manager for LatheOS.
You speak concisely. When the user requests a system action you MUST emit a JSON
object on a line by itself with the schema:
  {"action": "execute_bash"|"sway_msg"|"open_app"|"noop", "command": "<string>", "rationale": "<string>"}
Do not wrap the JSON in code fences. Keep spoken text brief; detailed reasoning
belongs in the `rationale` field.
"""


@dataclass(slots=True)
class LLMResponse:
    spoken: str
    command: Command | None


class LLMRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, user_text: str) -> LLMResponse:
        if self.settings.llm_provider == "xai" and self.settings.xai_api_key:
            provider, model = "xai", self.settings.xai_model
            caller = self._call_xai
        elif self.settings.groq_api_key:
            provider, model = "groq", self.settings.groq_model
            caller = self._call_groq
        else:
            provider, model = "mock", "mock"
            caller = None

        with span(
            "llm.complete",
            **{
                "llm.provider": provider,
                "llm.model": model,
                "llm.input_chars": len(user_text),
            },
        ) as s:
            raw = self._mock(user_text) if caller is None else await caller(user_text)
            parsed = self._parse(raw)
            s.set_attribute("llm.output_chars", len(raw))
            s.set_attribute("llm.has_command", parsed.command is not None)
            return parsed

    async def _call_groq(self, user_text: str) -> str:
        resp = await self._client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
            json={
                "model": self.settings.groq_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def _call_xai(self, user_text: str) -> str:
        resp = await self._client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.xai_api_key}"},
            json={
                "model": self.settings.xai_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _mock(user_text: str) -> str:
        if "backend" in user_text.lower():
            return (
                "Spinning up the backend now.\n"
                '{"action":"execute_bash","command":"docker compose up -d",'
                '"rationale":"user asked to start backend services"}'
            )
        return "Acknowledged."

    @staticmethod
    def _parse(raw: str) -> LLMResponse:
        spoken_lines: list[str] = []
        command: Command | None = None
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    payload = json.loads(stripped)
                    command = Command(**payload)
                    continue
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning("llm.parse.bad_json", error=str(exc), line=stripped)
            spoken_lines.append(line)
        return LLMResponse(spoken="\n".join(spoken_lines).strip(), command=command)
