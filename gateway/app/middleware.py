from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, NoReturn

from fastapi import Depends, Request
from pydantic import ValidationError

from .canonical import (
    CanonicalizationError,
    InvalidSignatureError,
    VoucherDomain,
    loads_strict,
    recover_voucher_signer,
    request_hash as compute_request_hash,
)
from .config import Settings
from .registry import STATUS_OPEN, ChainReader, ChainUnavailable
from .schemas import (
    REASON_CODES,
    AcceptOption,
    PaymentChallenge,
    PaymentTicket,
    PaymentVoucher,
    TaskExecutionRequest,
)
from .store import InProgress, Ledger, LedgerRejection, Replay, Reserved

log = logging.getLogger("amx.gateway")


//constants
TICKET_HEADER = "X-402-Payment-Ticket"
CHANNEL_HINT_HEADER = "X-AMX-Channel-Id"
_CHANNEL_ID_RE = re.compile(r"^0x[0-9a-f]{64}$") //0x-64 lowercase hexadecimal chars after it


# Errors
class PaymentRequired(Exception):
    """Carries the 402 challenge to the exception handler."""

    def __init__(self, challenge: PaymentChallenge) -> None:
        super().__init__(challenge.reason or "payment_required")
        self.challenge = challenge


class ApiError(Exception):
    """Non-402 error with a stable machine-readable code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.extra = extra or {}


# Shared application context (stored on app.state.amx)


class RateLimiter:
    """Sliding-window limiter keyed by client address.  In-memory: one process only."""

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 10_000) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> float | None:
        """Record a hit; return None if allowed, else seconds until the client may retry."""
        now = time.monotonic()
        if len(self._hits) >= self._max_keys:
            self._hits = {k: q for k, q in self._hits.items() if q and q[-1] > now - self._window}
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(0.0, hits[0] + self._window - now)
        hits.append(now)
        return None


@dataclass
class AppContext:
    settings: Settings
    chain: ChainReader
    ledger: Ledger
    limiter: RateLimiter

    @property
    def domain(self) -> VoucherDomain:
        return VoucherDomain(chain_id=self.settings.chain_id, verifying_contract=self.settings.channel_contract)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.amx


@dataclass(frozen=True)
class VerifiedPayment:
    request: TaskExecutionRequest
    request_hash: str
    voucher: PaymentVoucher
    signature: str
    signer: str
    reservation: Reserved | None  # set for a fresh, charged call
    replay: Replay | None  # set when an identical earlier call is being re-served (no new charge)


# Helpers


def client_key(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit(request: Request) -> None:
    ctx = get_ctx(request)
    retry_after = ctx.limiter.check(client_key(request, ctx.settings.trust_proxy_headers))
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Too many requests.",
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )


async def build_challenge(ctx: AppContext, channel_id: str | None, reason: REASON_CODES | None) -> PaymentChallenge:
    """Build the 402 body.  With a channel id we can state the exact next nonce / cumulative."""
    price = ctx.settings.price_per_call
    last_nonce, last_cumulative = 0, 0
    if channel_id is not None:
        state = await asyncio.to_thread(ctx.ledger.get_state, channel_id)
        last_nonce, last_cumulative = state.last_nonce, state.last_cumulative
    return PaymentChallenge(
        reason=reason,
        accepts=[
            AcceptOption(
                network=ctx.settings.chain_network,
                channelContract=ctx.settings.channel_contract,
                agentId=ctx.settings.agent_id,
                pricePerCall=str(price),
                minNonce=last_nonce + 1,
                minCumulativeAmount=str(last_cumulative + price),
            )
        ],
    )


async def refuse(ctx: AppContext, reason: REASON_CODES | None, channel_id: str | None) -> NoReturn:
    log.info("402 reason=%s channel=%s", reason, channel_id)
    raise PaymentRequired(await build_challenge(ctx, channel_id, reason))


async def read_body_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "body_too_large", f"Request body exceeds {limit} bytes.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "body_too_large", f"Request body exceeds {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def decode_ticket(header_value: str, max_bytes: int) -> PaymentTicket:
    """base64url(JSON{voucher, signature}) -> PaymentTicket.  Raises ValueError on any problem."""
    if len(header_value) > max_bytes:
        raise ValueError("ticket too large")
    try:
        data = base64.urlsafe_b64decode(header_value + "=" * (-len(header_value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ticket is not base64url") from exc
    try:
        payload = loads_strict(data)
        return PaymentTicket.model_validate(payload)
    except (CanonicalizationError, ValidationError) as exc:
        raise ValueError("ticket does not match the schema") from exc


# The dependency


async def verify_x402_payment(request: Request, _: None = Depends(rate_limit)) -> VerifiedPayment:
    ctx = get_ctx(request)
    s = ctx.settings

    raw = await read_body_limited(request, s.max_body_bytes)

    hint = request.headers.get(CHANNEL_HINT_HEADER)
    hint = hint if hint and _CHANNEL_ID_RE.match(hint) else None

    # 1. No ticket: state the price.  Done before body validation so clients can always discover it.
    header_value = request.headers.get(TICKET_HEADER)
    if not header_value:
        await refuse(ctx, None, hint)

    # 2. Body shape.  A bad body is a 422 and is never charged, whatever the ticket says.
    try:
        payload = loads_strict(raw)
        task_request = TaskExecutionRequest.model_validate(payload)
        req_hash = compute_request_hash(payload)  # hash of the canonical body AS SENT
    except CanonicalizationError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from exc
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_request",
            "Request body failed validation.",
            extra={"errors": exc.errors(include_url=False, include_context=False, include_input=False)},
        ) from exc

    # 3. Ticket shape.
    try:
        ticket = decode_ticket(header_value, s.max_ticket_bytes)
    except ValueError:
        await refuse(ctx, "malformed_ticket", hint)
    voucher = ticket.voucher
    channel_id = voucher.channelId

    # 4. Cheap local binding checks.
    if voucher.agentId != s.agent_id:
        await refuse(ctx, "wrong_agent", channel_id)
    if voucher.requestHash != req_hash:
        await refuse(ctx, "request_hash_mismatch", channel_id)

    # 5. Signature: must recover (65 bytes, v 27/28, low-s) - same rules the contract enforces.
    voucher_fields = voucher.model_dump()
    try:
        signer = recover_voucher_signer(ctx.domain, voucher_fields, ticket.signature)
    except InvalidSignatureError:
        await refuse(ctx, "invalid_signature", channel_id)

    cumulative = int(voucher.cumulativeAmount)

    # 6. Exact retry of a call we already served: hand back the original result, charge nothing.
    try:
        replay = await asyncio.to_thread(
            ctx.ledger.lookup_replay, channel_id, voucher.nonce, req_hash, cumulative, signer
        )
    except InProgress:
        raise ApiError(409, "request_in_progress", "An identical request is being processed.", headers={"Retry-After": "1"})
    if replay is not None:
        log.info("replay channel=%s nonce=%s", channel_id, voucher.nonce)
        return VerifiedPayment(task_request, req_hash, voucher, ticket.signature, signer, None, replay)

    # 7. Expiry (bounds how long the gateway accepts a voucher; not enforced on-chain).
    if voucher.expiry <= int(time.time()):
        await refuse(ctx, "expired", channel_id)

    # 8. On-chain: the agent must exist and its owner / signing key must match what we are configured as.
    try:
        agent = await ctx.chain.get_agent(s.agent_id)
        if agent is None:
            log.error("agent %s is not registered in %s", s.agent_id, s.identity_contract)
            raise ApiError(503, "agent_not_registered", "The served agent is not registered on-chain.")
        if agent.owner.lower() != s.agent_owner_address.lower() or agent.signer.lower() != s.agent_signer_address.lower():
            log.error(
                "registry mismatch for agent %s: onchain owner=%s signer=%s, configured owner=%s signer=%s",
                s.agent_id, agent.owner, agent.signer, s.agent_owner_address, s.agent_signer_address,
            )
            raise ApiError(503, "agent_registry_mismatch", "On-chain agent record does not match gateway configuration.")

        # 9. On-chain: the channel.
        channel = await ctx.chain.get_channel(channel_id)
    except ChainUnavailable as exc:
        log.error("chain unavailable: %s", exc)
        raise ApiError(503, "chain_unavailable", "Could not read chain state; try again shortly.", headers={"Retry-After": "2"}) from exc

    if channel is None:
        await refuse(ctx, "unknown_channel", channel_id)
    if channel.status != STATUS_OPEN:
        await refuse(ctx, "channel_not_open", channel_id)
    if channel.agent_id != s.agent_id:
        await refuse(ctx, "channel_agent_mismatch", channel_id)
    if channel.payer.lower() != signer.lower():
        await refuse(ctx, "signer_not_payer", channel_id)
    if channel.challenge_period < s.min_accepted_challenge_period:
        await refuse(ctx, "challenge_period_too_short", channel_id)
    if cumulative > channel.deposit:
        await refuse(ctx, "exceeds_deposit", channel_id)

    # 10. Replay protection: atomic, durable.
    try:
        outcome = await asyncio.to_thread(
            ctx.ledger.reserve,
            channel_id=channel_id,
            nonce=voucher.nonce,
            cumulative=cumulative,
            request_hash=req_hash,
            signer=signer,
            voucher_json=_voucher_json(voucher_fields),
            signature=ticket.signature,
            price=s.price_per_call,
        )
    except LedgerRejection as rejection:
        await refuse(ctx, rejection.reason, channel_id)
    except InProgress:
        raise ApiError(409, "request_in_progress", "An identical request is being processed.", headers={"Retry-After": "1"})

    if isinstance(outcome, Replay):
        return VerifiedPayment(task_request, req_hash, voucher, ticket.signature, signer, None, outcome)
    return VerifiedPayment(task_request, req_hash, voucher, ticket.signature, signer, outcome, None)


def _voucher_json(fields: dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))