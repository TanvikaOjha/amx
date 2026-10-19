"""Wire schemas (Pydantic v2, strict, extra="forbid").

Conventions
  * Voucher / ticket / challenge fields are camelCase because they mirror the EIP-712 struct
    and the public x402-style challenge body.  Request/response fields are snake_case.
  * uint256 amounts travel as decimal STRINGS (JSON numbers lose precision above 2**53).
    agentId, nonce and expiry are JSON integers limited to the JS-safe range.
  * bytes32 values are 0x + 64 LOWERCASE hex chars, so a value has exactly one spelling.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

MAX_SAFE_INT = 2**53 - 1
UINT256_MAX = 2**256 - 1

REASON_CODES = Literal[
    "malformed_ticket",
    "wrong_agent",
    "request_hash_mismatch",
    "expired",
    "invalid_signature",
    "unknown_channel",
    "channel_not_open",
    "channel_agent_mismatch",
    "signer_not_payer",
    "challenge_period_too_short",
    "exceeds_deposit",
    "nonce_not_increasing",
    "insufficient_cumulative_amount",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _check_uint256(value: str) -> str:
    if int(value) > UINT256_MAX:
        raise ValueError("exceeds uint256")
    return value


Bytes32Hex = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-f]{64}$")]
SignatureHex = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{130}$")]
AddressHex = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
DecimalUint256 = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]{0,77})$"),
    AfterValidator(_check_uint256),
]
SafeUInt = Annotated[int, Field(ge=0, le=MAX_SAFE_INT)]
PositiveSafeUInt = Annotated[int, Field(ge=1, le=MAX_SAFE_INT)]


# Payment ticket (header X-402-Payment-Ticket = base64url(JSON{voucher, signature}))


class PaymentVoucher(StrictModel):
    channelId: Bytes32Hex
    agentId: PositiveSafeUInt
    cumulativeAmount: DecimalUint256
    nonce: PositiveSafeUInt
    expiry: PositiveSafeUInt
    requestHash: Bytes32Hex


class PaymentTicket(StrictModel):
    voucher: PaymentVoucher
    signature: SignatureHex


# 402 challenge body


class AcceptOption(StrictModel):
    scheme: Literal["amx-voucher"] = "amx-voucher"
    network: str
    channelContract: AddressHex
    agentId: PositiveSafeUInt
    pricePerCall: DecimalUint256
    asset: Literal["native"] = "native"
    minNonce: PositiveSafeUInt
    minCumulativeAmount: DecimalUint256


class PaymentChallenge(StrictModel):
    error: Literal["payment_required"] = "payment_required"
    # Machine-readable reason a presented ticket was refused; absent when no ticket was sent.
    reason: REASON_CODES | None = None
    accepts: list[AcceptOption]


# Task execution

ParamKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]  # ASCII-only: stable key order
ParamValue = Annotated[str, StringConstraints(max_length=1024)] | bool | Annotated[
    int, Field(ge=-MAX_SAFE_INT, le=MAX_SAFE_INT)
]


class TaskExecutionRequest(StrictModel):
    task: Literal["echo", "sha256", "word_count"]
    input: Annotated[str, StringConstraints(max_length=8192)]
    params: Annotated[dict[ParamKey, ParamValue], Field(max_length=16)] = Field(default_factory=dict)


class TaskExecutionResponse(StrictModel):
    job_id: str
    task: str
    output: str
    request_hash: Bytes32Hex
    response_hash: Bytes32Hex
    channel_id: Bytes32Hex
    nonce: PositiveSafeUInt
    cumulative_amount: DecimalUint256
    charged_amount: DecimalUint256
    idempotent_replay: bool = False
    # Filled in by Phase 3 (Hedera Consensus Service).
    hcs_status: Literal["disabled", "pending", "confirmed", "failed"] = "disabled"
    hcs_topic_id: str | None = None
    hcs_sequence_number: SafeUInt | None = None


# Misc responses


class ChannelStatusResponse(StrictModel):
    channelId: Bytes32Hex
    lastNonce: SafeUInt
    lastCumulativeAmount: DecimalUint256
    minNonce: PositiveSafeUInt
    minCumulativeAmount: DecimalUint256


class HealthResponse(StrictModel):
    status: Literal["ok", "degraded"]
    agentId: PositiveSafeUInt
    network: str
    registry: str
    hcs: str = "disabled"