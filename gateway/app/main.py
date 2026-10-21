"""AMX gateway application (HTTP 402 gate + payment ledger + Hedera HCS audit log).

Run (from gateway/):
    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

HCS wiring (Phase 3)
  * `create_app(settings, chain, hcs)`: pass an HcsPublisher explicitly (tests do), or pass nothing.
    ONLY when the app builds its own settings (the production `--factory` path) does it read
    OPERATOR_ID / OPERATOR_KEY / TOPIC_ID from the environment.  Explicit settings never pick up
    Hedera credentials, so a developer's real variables cannot make unit tests talk to Hedera.
  * Order of events for a paid call:
        reserve voucher -> run job -> store the response (hcs_status="pending") -> publish proof
    The job result is made durable BEFORE Hedera is involved, so a Hedera problem can never lose it.
    When consensus is reached later (even after a restart) the publisher's callback patches the
    stored response, so an idempotent retry reports the sequence number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Path as PathParam, Request, Response
from fastapi.responses import JSONResponse

from .canonical import canonical_hash
from .config import Settings, get_settings
from .hedera_logger import HcsOutcome, HcsPublisher, build_proof, build_publisher_from_env, encode_proof
from .middleware import (
    ApiError,
    AppContext,
    PaymentRequired,
    RateLimiter,
    VerifiedPayment,
    get_ctx,
    rate_limit,
    verify_x402_payment,
)
from .registry import ChainReader, ChainUnavailable, Web3ChainReader
from .schemas import (
    ChannelStatusResponse,
    HealthResponse,
    PaymentChallenge,
    TaskExecutionRequest,
    TaskExecutionResponse,
)
from .store import Ledger

log = logging.getLogger("amx.gateway")

AGENT_CARD_TEMPLATE = Path(__file__).resolve().parent.parent / "agent-card.json"


def _configure_logging() -> None:
    amx_logger = logging.getLogger("amx")
    if not amx_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        amx_logger.addHandler(handler)
    amx_logger.setLevel(logging.INFO)
    amx_logger.propagate = False


# ---------------------------------------------------------------------------
# The "work": small deterministic built-in tasks so the payment flow is testable end to end.
# ---------------------------------------------------------------------------


def run_task(request: TaskExecutionRequest) -> str:
    if request.task == "echo":
        return request.input
    if request.task == "sha256":
        return hashlib.sha256(request.input.encode("utf-8")).hexdigest()
    if request.task == "word_count":
        return str(len(request.input.split()))
    raise ValueError(f"unsupported task: {request.task}")  # unreachable: schema restricts `task`


def build_agent_card(settings: Settings) -> dict[str, Any]:
    """Serve the static template with the live, config-derived fields filled in."""
    card = json.loads(AGENT_CARD_TEMPLATE.read_text(encoding="utf-8"))
    card["services"] = [{"name": "amx-execute", "endpoint": f"{settings.public_base_url}/v1/execute", "version": "1"}]
    card["registrations"] = [{"agentId": settings.agent_id, "agentRegistry": settings.agent_registry_id}]
    card["amx"] = {
        "paymentScheme": "amx-voucher",
        "network": settings.chain_network,
        "chainId": settings.chain_id,
        "channelContract": settings.channel_contract,
        "asset": "native",
        "pricePerCall": str(settings.price_per_call),
        "ticketHeader": "X-402-Payment-Ticket",
        "channelHintHeader": "X-AMX-Channel-Id",
        "signingKey": {"type": "secp256k1", "publicKey": settings.agent_pubkey_hex, "address": settings.agent_signer_address},
    }
    return card


# ---------------------------------------------------------------------------
# HCS helpers
# ---------------------------------------------------------------------------


def _with_outcome(response: TaskExecutionResponse, outcome: HcsOutcome) -> TaskExecutionResponse:
    """The same response with its HCS fields replaced by `outcome` (model is frozen, so copy)."""
    return response.model_copy(
        update={
            "hcs_status": outcome.status,
            "hcs_topic_id": outcome.topic_id or response.hcs_topic_id,
            "hcs_sequence_number": outcome.sequence_number,
        }
    )


async def _publish_proof(ctx: AppContext, result: TaskExecutionResponse) -> TaskExecutionResponse:
    """Queue the compact audit proof and wait (bounded) for consensus.  Never raises on Hedera trouble:
    the job is already stored and charged, so the worst case is hcs_status "pending" / "failed"."""
    hcs = ctx.hcs
    assert hcs is not None
    try:
        proof = build_proof(
            ctx.settings.agent_private_key.get_secret_value(),
            agent_id=ctx.settings.agent_id,
            channel_id=result.channel_id,
            nonce=result.nonce,
            request_hash=result.request_hash,
            response_hash=result.response_hash,
            ts=int(time.time()),
        )
        outcome = await hcs.publish(result.channel_id, result.nonce, encode_proof(proof))
    except asyncio.CancelledError:  # client went away: the job stays charged, the proof (if queued) still goes out
        raise
    except Exception:
        log.exception("could not queue the HCS proof for channel=%s nonce=%s", result.channel_id, result.nonce)
        outcome = HcsOutcome("failed", hcs.topic_id, None)

    updated = _with_outcome(result, outcome)
    if outcome.status != "pending":  # normally the publisher callback already did this; this makes it certain
        try:
            await asyncio.to_thread(ctx.ledger.update_response, result.channel_id, result.nonce, updated.model_dump_json())
        except Exception:
            log.exception("could not persist the HCS outcome for channel=%s nonce=%s", result.channel_id, result.nonce)
    return updated


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    chain: ChainReader | None = None,
    hcs: HcsPublisher | None = None,
) -> FastAPI:
    _configure_logging()
    explicit_settings = settings is not None
    settings = settings or get_settings()
    if chain is None:
        chain = Web3ChainReader(
            settings.rpc_url,
            settings.identity_contract,
            settings.channel_contract,
            agent_ttl=settings.registry_cache_ttl_seconds,
            channel_ttl=settings.chain_cache_ttl_seconds,
            timeout=settings.rpc_timeout_seconds,
        )
    if hcs is None and not explicit_settings:
        hcs = build_publisher_from_env(settings.db_path)  # None when Hedera is wholly unconfigured

    ledger = Ledger(settings.db_path)

    if hcs is not None:

        async def on_hcs_outcome(channel_id: str, nonce: int, outcome: HcsOutcome) -> None:
            """Consensus reached (or given up) possibly long after the request: patch the stored response."""
            stored = await asyncio.to_thread(ledger.get_response, channel_id, nonce)
            if stored is None:
                return
            updated = _with_outcome(TaskExecutionResponse.model_validate_json(stored), outcome)
            await asyncio.to_thread(ledger.update_response, channel_id, nonce, updated.model_dump_json())

        hcs.on_outcome = on_hcs_outcome

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if hcs is not None:
            await hcs.start()  # also resumes proofs left pending by a previous run
        try:
            yield
        finally:
            if hcs is not None:
                await hcs.stop()
            await chain.aclose()

    app = FastAPI(title="Agent-Mesh-X Gateway", version="0.3.0", lifespan=lifespan)
    app.state.amx = AppContext(
        settings=settings,
        chain=chain,
        ledger=ledger,
        limiter=RateLimiter(settings.rate_limit_per_minute),
        hcs=hcs,
    )
    app.state.hcs = hcs

    # -- error handlers -----------------------------------------------------------
    @app.exception_handler(PaymentRequired)
    async def _payment_required(_: Request, exc: PaymentRequired) -> JSONResponse:
        return JSONResponse(
            status_code=402,
            content=exc.challenge.model_dump(mode="json", exclude_none=True),
            headers={"WWW-Authenticate": f'amx-voucher realm="agent-{settings.agent_id}"'},
        )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.code, "message": exc.message, **exc.extra}
        return JSONResponse(status_code=exc.status, content=body, headers=exc.headers)

    # -- routes -------------------------------------------------------------------
    @app.post(
        "/v1/execute",
        response_model=TaskExecutionResponse,
        responses={402: {"model": PaymentChallenge, "description": "Payment required / ticket rejected"}},
    )
    async def execute(response: Response, payment: VerifiedPayment = Depends(verify_x402_payment)) -> TaskExecutionResponse:
        ctx: AppContext = app.state.amx

        if payment.replay is not None:  # identical retry: original result, no second charge
            stored = TaskExecutionResponse.model_validate_json(payment.replay.response_json)
            response.headers["X-AMX-Idempotent-Replay"] = "true"
            return stored.model_copy(update={"idempotent_replay": True})

        reservation = payment.reservation
        assert reservation is not None  # verify_x402_payment returns exactly one of replay / reservation
        try:
            output = run_task(payment.request)
            job_id = uuid.uuid4().hex
            result = TaskExecutionResponse(
                job_id=job_id,
                task=payment.request.task,
                output=output,
                request_hash=payment.request_hash,
                response_hash=canonical_hash(
                    {"job_id": job_id, "task": payment.request.task, "output": output, "request_hash": payment.request_hash}
                ),
                channel_id=reservation.channel_id,
                nonce=reservation.nonce,
                cumulative_amount=str(reservation.cumulative),
                charged_amount=str(settings.price_per_call),
                hcs_status="pending" if ctx.hcs is not None else "disabled",
                hcs_topic_id=ctx.hcs.topic_id if ctx.hcs is not None else None,
            )
            # Durable BEFORE Hedera is involved: from here on the job can no longer be lost.
            await asyncio.to_thread(ctx.ledger.complete, reservation.channel_id, reservation.nonce, result.model_dump_json())
        except asyncio.CancelledError:  # client went away mid-job: do not leave the voucher reserved
            ctx.ledger.release(reservation.channel_id, reservation.nonce)
            raise
        except Exception as exc:
            log.exception("job failed; releasing reservation channel=%s nonce=%s", reservation.channel_id, reservation.nonce)
            ctx.ledger.release(reservation.channel_id, reservation.nonce)  # undo the charge: client may retry the same voucher
            raise ApiError(500, "execution_failed", "The task failed; you were not charged.") from exc

        if ctx.hcs is not None:
            result = await _publish_proof(ctx, result)

        log.info(
            "200 job=%s channel=%s nonce=%s cumulative=%s hcs=%s seq=%s",
            result.job_id, result.channel_id, result.nonce, result.cumulative_amount, result.hcs_status, result.hcs_sequence_number,
        )
        return result

    @app.get("/v1/channels/{channel_id}", response_model=ChannelStatusResponse, dependencies=[Depends(rate_limit)])
    async def channel_status(
        channel_id: str = PathParam(pattern=r"^0x[0-9a-f]{64}$"), ctx: AppContext = Depends(get_ctx)
    ) -> ChannelStatusResponse:
        """What the NEXT voucher on this channel must satisfy (from the gateway's ledger)."""
        state = await asyncio.to_thread(ctx.ledger.get_state, channel_id)
        price = ctx.settings.price_per_call
        return ChannelStatusResponse(
            channelId=channel_id,
            lastNonce=state.last_nonce,
            lastCumulativeAmount=str(state.last_cumulative),
            minNonce=state.last_nonce + 1,
            minCumulativeAmount=str(state.last_cumulative + price),
        )

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> dict[str, Any]:
        return build_agent_card(settings)

    @app.get("/health", response_model=HealthResponse)
    async def health(ctx: AppContext = Depends(get_ctx)) -> HealthResponse:
        registry = "ok"
        try:
            agent = await ctx.chain.get_agent(settings.agent_id)
            if agent is None:
                registry = "agent_not_registered"
            elif (
                agent.owner.lower() != settings.agent_owner_address.lower()
                or agent.signer.lower() != settings.agent_signer_address.lower()
            ):
                registry = "agent_registry_mismatch"
        except ChainUnavailable:
            registry = "chain_unavailable"

        hcs_state, hcs_failed = "disabled", False
        if ctx.hcs is not None:
            try:
                counts = await ctx.hcs.stats()
                hcs_state = "ok" if not (counts["pending"] or counts["failed"]) else f"pending:{counts['pending']},failed:{counts['failed']}"
                hcs_failed = counts["failed"] > 0  # a proof that could not be delivered needs a human
            except Exception:
                log.exception("could not read the HCS outbox")
                hcs_state, hcs_failed = "unavailable", True

        return HealthResponse(
            status="ok" if registry == "ok" and not hcs_failed else "degraded",
            agentId=settings.agent_id,
            network=settings.chain_network,
            registry=registry,
            hcs=hcs_state,
        )

    return app