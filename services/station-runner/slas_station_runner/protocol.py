"""The wire between the factory executor and a station runner: signed batches, results.

A batch is one compiled skill (GUI steps for the station's screen), one station command
(argv, allowlisted on the station), or a request for the station's state (for the backup).
The executor signs the canonical JSON of the batch with a per-station key (HMAC-SHA256, key
by reference); the runner verifies the signature, the expiry and that it has not seen the
batch id before. mTLS is the transport (`server.py`); the signature is what the runner
trusts even if a proxy sits in between. Ed25519 signatures replace HMAC once `cryptography`
is an approved dependency; the shape of the messages does not change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field

from slas_hal.hal import CommandResult
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage
from slas_skills.compiler import CompiledSkill
from slas_skills.runner import SkillRunResult

BatchKind = Literal["skill", "command", "state"]


class BatchError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class StepBatch(SlasModel):
    batch_id: str = Field(min_length=8)
    ticket_id: str = Field(min_length=1)
    station: str = Field(min_length=1)
    issued_at: datetime
    ttl_s: int = Field(default=300, ge=1, le=3600)
    kind: BatchKind
    #: kind == "skill": the compiled steps to perform on the station's screen.
    compiled: CompiledSkill | None = None
    approvals: list[str] = Field(default_factory=list)
    #: Secret handle → value, resolved by the executor for this batch only; never journalled.
    secrets: dict[str, str] = Field(default_factory=dict)
    #: kind == "command": argv to run on the station (allowlisted there).
    command: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=600, ge=1, le=7200)

    def expires_at(self) -> datetime:
        return self.issued_at + timedelta(seconds=self.ttl_s)

    def canonical(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


class SignedBatch(SlasModel):
    batch: StepBatch
    key_id: str = Field(min_length=1)
    algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


def sign_batch(batch: StepBatch, *, key_id: str, key: bytes) -> SignedBatch:
    digest = hmac.new(key, batch.canonical(), hashlib.sha256).hexdigest()
    return SignedBatch(batch=batch, key_id=key_id, signature=digest)


def verify_batch(
    signed: SignedBatch,
    *,
    keys: Mapping[str, bytes],
    station: str,
    now: datetime,
    seen: set[str],
) -> StepBatch:
    """The runner's gate: right station, known key, valid signature, not expired, not replayed."""
    batch = signed.batch
    if batch.station != station:
        raise BatchError(
            ThreePartMessage(
                f"This batch is addressed to {batch.station}, not to {station}.",
                "The executor sent it to the wrong runner, or a runner is misconfigured.",
                "Check the station name on the target record and on the runner.",
            )
        )
    key = keys.get(signed.key_id)
    if key is None:
        raise BatchError(
            ThreePartMessage(
                f"The batch is signed with an unknown key ({signed.key_id}).",
                "The runner only trusts the keys provisioned on the station.",
                "Provision the executor's key on the station, or rotate both sides.",
            )
        )
    expected = hmac.new(key, batch.canonical(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signed.signature):
        raise BatchError(
            ThreePartMessage(
                "The batch signature does not match its content.",
                "The batch was altered in transit or signed with a different key.",
                "Nothing was performed. Check the path between the executor and the station.",
            )
        )
    if now > batch.expires_at():
        raise BatchError(
            ThreePartMessage(
                f"The batch expired at {batch.expires_at().isoformat()}.",
                "Batches are valid for a few minutes so a captured one cannot be replayed later.",
                "Nothing was performed. Start the step again.",
            )
        )
    if batch.batch_id in seen:
        raise BatchError(
            ThreePartMessage(
                f"The batch {batch.batch_id} was already performed.",
                "A batch id is accepted once; this is a replay or a duplicate send.",
                "Nothing was performed again. Check the executor's journal for the first result.",
            )
        )
    seen.add(batch.batch_id)
    return batch


class Screenshot(SlasModel):
    name: str = Field(min_length=1)
    png_base64: str = Field(min_length=1)


class StateSnapshot(SlasModel):
    """What a station backup carries: config files, recent logs and application versions."""

    files: dict[str, str] = Field(default_factory=dict)
    versions: dict[str, str] = Field(default_factory=dict)
    taken_at: datetime


class BatchResult(SlasModel):
    batch_id: str
    kind: BatchKind
    ok: bool
    sentence: str = Field(min_length=1)
    skill: SkillRunResult | None = None
    command: CommandResult | None = None
    state: StateSnapshot | None = None
    screenshots: list[Screenshot] = Field(default_factory=list)


def new_batch_id(ticket_id: str, step_id: str, n: int) -> str:
    return hashlib.sha256(f"{ticket_id}|{step_id}|{n}".encode()).hexdigest()[:24]


def three_part(data: Mapping[str, Any]) -> ThreePartMessage:
    return ThreePartMessage(
        str(data.get("what_happened") or "The station runner refused the request."),
        str(data.get("likely_cause") or "It gave no reason."),
        str(data.get("what_to_do") or "Check the runner's log on the station."),
    )
