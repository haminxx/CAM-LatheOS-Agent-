"""Wire-format contracts between LatheOS and CAM.

The socket carries two channels interleaved:
    - Binary frames  -> raw PCM (uplink) or Cartesia audio chunks (downlink)
    - Text frames    -> JSON control messages defined here
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Hello(BaseModel):
    """Client -> Server. First message after socket opens."""

    type: Literal["hello"] = "hello"
    hardware_token: str
    sample_rate: int = 16_000
    encoding: Literal["linear16", "opus"] = "linear16"
    client_version: str


class Transcript(BaseModel):
    """Server -> Client. Partial or final STT result."""

    type: Literal["transcript"] = "transcript"
    text: str
    is_final: bool
    confidence: float = 0.0


class Command(BaseModel):
    """Server -> Client. Structured action for the local executor."""

    type: Literal["command"] = "command"
    action: Literal["execute_bash", "sway_msg", "open_app", "noop"]
    command: str = ""
    rationale: str = ""


class SpeechStart(BaseModel):
    """Server -> Client. Marks beginning of a TTS audio stream."""

    type: Literal["speech_start"] = "speech_start"
    utterance_id: str
    sample_rate: int = 24_000
    encoding: Literal["pcm_s16le", "opus"] = "pcm_s16le"


class SpeechEnd(BaseModel):
    type: Literal["speech_end"] = "speech_end"
    utterance_id: str


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


ServerEvent = Annotated[
    Transcript | Command | SpeechStart | SpeechEnd | ErrorMsg,
    Field(discriminator="type"),
]
