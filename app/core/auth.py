"""Hardware-token verification against DynamoDB.

Each LatheOS NVMe drive ships with a unique hardware token burned into
its immutable Nix store. The proxy validates that token before any
audio flows — no token, no socket, no bytes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class HardwareIdentity:
    token: str
    user_id: str
    quota_remaining: int
    tier: str


class AuthError(Exception):
    """Raised when a hardware token cannot be verified."""


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.dynamodb_table
        )

    async def verify(self, token: str) -> HardwareIdentity:
        if self._settings.allow_unverified_tokens and self._settings.env == "dev":
            log.warning("auth.bypass", token=token[:8])
            return HardwareIdentity(
                token=token, user_id="dev-user", quota_remaining=10_000, tier="dev"
            )

        if not token:
            raise AuthError("missing hardware token")

        try:
            response = await asyncio.to_thread(self._table.get_item, Key={"token": token})
        except (BotoCoreError, ClientError) as exc:
            log.error("auth.dynamodb_error", error=str(exc))
            raise AuthError("verification backend unavailable") from exc

        item = response.get("Item")
        if not item:
            raise AuthError("unknown hardware token")

        if int(item.get("quota_remaining", 0)) <= 0:
            raise AuthError("quota exhausted")

        return HardwareIdentity(
            token=token,
            user_id=item["user_id"],
            quota_remaining=int(item["quota_remaining"]),
            tier=item.get("tier", "standard"),
        )
