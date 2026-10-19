"""AMX gateway application.

Run (from gateway/):
    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Path as PathParam, Request, Response
from fastapi.responses import JSONResponse

from .canonical import canonical_hash
from .config import Settings, get_settings
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
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None, chain: ChainReader | None = None) -> FastAPI:
    _configure_logging()
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await chain.aclose()

    app = FastAPI(title="Agent-Mesh-X Gateway", version="0.2.0", lifespan=lifespan)
    app.state.amx = AppContext(
        settings=settings,
        chain=chain,
        ledger=Ledger(settings.db_path),
        limiter=RateLimiter(settings.rate_limit_per_minute),
    )

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
        ctx = app.state.amx

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
            )
            await asyncio.to_thread(ctx.ledger.complete, reservation.channel_id, reservation.nonce, result.model_dump_json())
        except asyncio.CancelledError:  # client went away mid-job: do not leave the voucher reserved
            ctx.ledger.release(reservation.channel_id, reservation.nonce)
            raise
        except Exception as exc:
            log.exception("job failed; releasing reservation channel=%s nonce=%s", reservation.channel_id, reservation.nonce)
            ctx.ledger.release(reservation.channel_id, reservation.nonce)  # undo the charge: client may retry the same voucher
            raise ApiError(500, "execution_failed", "The task failed; you were not charged.") from exc

        log.info("200 job=%s channel=%s nonce=%s cumulative=%s", result.job_id, result.channel_id, result.nonce, result.cumulative_amount)
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
        return HealthResponse(
            status="ok" if registry == "ok" else "degraded",
            agentId=settings.agent_id,
            network=settings.chain_network,
            registry=registry,
        )

    return app