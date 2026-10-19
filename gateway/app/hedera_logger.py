"""Hedera Consensus Service audit logging (Layer 4).

After each paid job the gateway publishes a COMPACT proof to an HCS topic:

    {agentId, channelId, nonce, requestHash, responseHash, ts, gatewaySig}

Never task content (HCS messages are public and capped at 1024 bytes).  `gatewaySig` is an
EIP-191 signature, by the agent's registered key, over sha256(canonical JSON of the other
fields); anyone can check it against `ERC8004Identity.signerOf(agentId)`.

Delivery model (why the code looks the way it does)
  * Hedera SDK calls are BLOCKING and take seconds (they wait for consensus), so they run in a
    worker thread, one at a time, never on the event loop.
  * A durable SQLite *outbox* holds every proof.  A single background worker is the ONLY thing that
    talks to Hedera.  A request merely *waits* (bounded) for its row to be confirmed.  Because
    the request path never submits on its own, a request timeout can never cause a duplicate
    submission while the first attempt is still in flight.
  * If Hedera is slow or down the request still returns 200 with hcs_status="pending"; the
    worker retries with exponential backoff, survives restarts (SQLite), and patches the stored
    response when the sequence number finally arrives.
  * Semantics are AT-LEAST-ONCE: if a network fault hides a successful submission the retry can
    produce a second message.  Consumers dedupe on (channelId, nonce).

SDK facts this module relies on (hiero-sdk-python 0.3.0, checked against the installed package):
  * Transaction.execute(client, timeout, wait_for_receipt=True, validate_status=False) returns a
    receipt and does NOT raise on a failure status unless validate_status=True -> we pass True and
    re-check receipt.status ourselves.
  * PrivateKey.from_string() treats a raw 32-byte key as Ed25519 -> raw keys need an explicit type.
  * Network("testnet") performs network I/O (fetches the node list) -> the client is created lazily,
    in the worker thread, never at import or on the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .canonical import canonical_bytes, sha256_hex

log = logging.getLogger("amx.hcs")

HCS_MAX_MESSAGE_BYTES = 1024
_ACCOUNT_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PROOF_FIELDS = ("agentId", "channelId", "nonce", "requestHash", "responseHash", "ts", "gatewaySig")

MIRROR_BASE_URLS = {
    "testnet": "https://testnet.mirrornode.hedera.com",
    "previewnet": "https://previewnet.mirrornode.hedera.com",
}
HASHSCAN_URLS = {"testnet": "https://hashscan.io/testnet", "previewnet": "https://hashscan.io/previewnet"}


# ---------------------------------------------------------------------------
# Settings (separate from gateway Settings so create_topic.py needs no chain config)
# ---------------------------------------------------------------------------


class HederaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    operator_id: str | None = None
    operator_key: SecretStr | None = None
    # Required when OPERATOR_KEY is a raw 32-byte hex key (DER-encoded keys are self-describing).
    operator_key_type: Literal["ecdsa", "ed25519"] | None = None
    # Testnets only, by design.  Mainnet is deliberately not an accepted value.
    hedera_network: Literal["testnet", "previewnet"] = "testnet"
    topic_id: str | None = None

    hcs_wait_seconds: float = Field(default=8.0, gt=0)  # how long a request waits for consensus
    hcs_submit_timeout_seconds: float = Field(default=30.0, gt=0)  # one SDK submission attempt
    hcs_max_attempts: int = Field(default=12, ge=1)
    hcs_retry_base_seconds: float = Field(default=2.0, gt=0)
    hcs_retry_max_seconds: float = Field(default=300.0, gt=0)

    @field_validator("operator_id", "topic_id")
    @classmethod
    def _entity_id(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                return None
            if not _ACCOUNT_RE.match(value):
                raise ValueError("must look like 0.0.12345")
        return value

    @model_validator(mode="after")
    def _key_needs_type_when_raw(self) -> "HederaSettings":
        if self.operator_key is not None:
            raw = _strip_hex(self.operator_key.get_secret_value())
            if raw and len(raw) == 64 and self.operator_key_type is None:
                raise ValueError(
                    "OPERATOR_KEY is a raw 32-byte key: set OPERATOR_KEY_TYPE to 'ecdsa' or 'ed25519' "
                    "(the SDK would silently assume ed25519, which fails with INVALID_SIGNATURE for ECDSA accounts)"
                )
        return self

    @property
    def has_operator(self) -> bool:
        return bool(self.operator_id and self.operator_key and self.operator_key.get_secret_value().strip())

    @property
    def has_topic(self) -> bool:
        return bool(self.topic_id)


def _strip_hex(value: str) -> str:
    value = value.strip()
    return value[2:] if value.lower().startswith("0x") else value


def load_operator_key(raw_key: str, key_type: str | None):
    """Load a Hedera operator key without ever guessing the algorithm of a raw key."""
    from hiero_sdk_python import PrivateKey  # imported lazily: the SDK is heavy and optional when HCS is off

    cleaned = _strip_hex(raw_key)
    try:
        bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("operator key is not valid hex") from exc  # never echo the key
    if len(cleaned) == 64:  # raw 32 bytes
        if key_type == "ecdsa":
            return PrivateKey.from_string_ecdsa(cleaned)
        if key_type == "ed25519":
            return PrivateKey.from_string_ed25519(cleaned)
        raise ValueError("raw operator key needs OPERATOR_KEY_TYPE=ecdsa|ed25519")
    return PrivateKey.from_string_der(cleaned)  # PKCS#8 DER is self-describing


def make_sdk_client(hs: HederaSettings):
    """Build an operator-configured SDK client.  Does network I/O: call from a worker thread."""
    from hiero_sdk_python import AccountId, Client, Network

    if not hs.has_operator:
        raise ValueError("OPERATOR_ID and OPERATOR_KEY are required")
    client = Client(Network(hs.hedera_network))
    client.set_operator(
        AccountId.from_string(hs.operator_id or ""),
        load_operator_key(hs.operator_key.get_secret_value() if hs.operator_key else "", hs.operator_key_type),
    )
    return client


# ---------------------------------------------------------------------------
# Proof construction / verification
# ---------------------------------------------------------------------------


class ProofError(ValueError):
    pass


def _proof_digest(unsigned: Mapping[str, Any]) -> bytes:
    return bytes.fromhex(sha256_hex(canonical_bytes(dict(unsigned)))[2:])


def build_proof(
    agent_private_key: str,
    *,
    agent_id: int,
    channel_id: str,
    nonce: int,
    request_hash: str,
    response_hash: str,
    ts: int,
) -> dict[str, Any]:
    unsigned = {
        "agentId": agent_id,
        "channelId": channel_id,
        "nonce": nonce,
        "requestHash": request_hash,
        "responseHash": response_hash,
        "ts": ts,
    }
    signed = Account.sign_message(encode_defunct(primitive=_proof_digest(unsigned)), private_key=agent_private_key)
    return {**unsigned, "gatewaySig": "0x" + bytes(signed.signature).hex()}


def encode_proof(proof: Mapping[str, Any]) -> str:
    """Canonical JSON text of the proof; refuses anything HCS could not carry in one message."""
    text = canonical_bytes(dict(proof)).decode("utf-8")
    if len(text.encode("utf-8")) > HCS_MAX_MESSAGE_BYTES:
        raise ProofError(f"proof is {len(text.encode('utf-8'))} bytes; HCS limit is {HCS_MAX_MESSAGE_BYTES}")
    return text


def verify_proof(proof: Mapping[str, Any], expected_signer: str | None = None) -> str:
    """Return the checksummed signer of a proof; raise ProofError if malformed or the signer differs."""
    if set(proof.keys()) != set(_PROOF_FIELDS):
        raise ProofError(f"proof must have exactly the fields {_PROOF_FIELDS}")
    unsigned = {k: proof[k] for k in _PROOF_FIELDS if k != "gatewaySig"}
    sig_text = proof["gatewaySig"]
    if not isinstance(sig_text, str):
        raise ProofError("gatewaySig must be a hex string")
    try:
        signature = bytes.fromhex(sig_text.removeprefix("0x"))
    except ValueError as exc:
        raise ProofError("gatewaySig is not hex") from exc
    if len(signature) != 65:
        raise ProofError("gatewaySig must be 65 bytes")
    try:
        recovered = Account.recover_message(encode_defunct(primitive=_proof_digest(unsigned)), signature=signature)
    except Exception as exc:  # input comes from a public topic: whatever the crypto layer raises, report ProofError
        raise ProofError("gatewaySig is not a valid signature over this proof") from exc
    recovered = to_checksum_address(recovered)
    if expected_signer is not None and recovered.lower() != expected_signer.lower():
        raise ProofError(f"signed by {recovered}, expected {expected_signer}")
    return recovered


# ---------------------------------------------------------------------------
# Mirror-node helpers (pure; the HTTP call lives in scripts/verify_hcs_message.py)
# ---------------------------------------------------------------------------


def mirror_message_url(network: str, topic_id: str, sequence_number: int) -> str:
    return f"{MIRROR_BASE_URLS[network]}/api/v1/topics/{topic_id}/messages/{sequence_number}"


def decode_mirror_message(payload: Mapping[str, Any]) -> bytes:
    """Mirror node returns the message body base64-encoded under "message"."""
    try:
        return base64.b64decode(payload["message"], validate=True)
    except (KeyError, ValueError) as exc:
        raise ProofError("mirror node response has no valid base64 'message'") from exc


# ---------------------------------------------------------------------------
# Hedera SDK wrapper
# ---------------------------------------------------------------------------


class HcsError(Exception):
    """A submission attempt failed (retryable by the outbox worker)."""


@dataclass(frozen=True)
class HcsReceipt:
    topic_id: str
    sequence_number: int


class AuditLogger(Protocol):
    topic_id: str

    async def submit(self, message: bytes) -> HcsReceipt: ...

    async def aclose(self) -> None: ...


class HederaAuditLogger:
    """Submits one message to the configured topic via hiero-sdk-python."""

    def __init__(self, hs: HederaSettings) -> None:
        if not (hs.has_operator and hs.has_topic):
            raise ValueError("HederaAuditLogger needs OPERATOR_ID, OPERATOR_KEY and TOPIC_ID")
        self._hs = hs
        self.topic_id = hs.topic_id or ""
        self._client: Any = None
        self._lock = threading.Lock()  # SDK thread-safety is not documented: one submission at a time

    # -- blocking part (runs in a worker thread) ------------------------------------
    def _make_client(self) -> Any:
        return make_sdk_client(self._hs)

    def _submit_sync(self, message: bytes) -> HcsReceipt:
        from hiero_sdk_python import ResponseCode, TopicId, TopicMessageSubmitTransaction

        with self._lock:
            if self._client is None:
                self._client = self._make_client()
            transaction = TopicMessageSubmitTransaction(
                topic_id=TopicId.from_string(self.topic_id),
                message=message,
                max_chunks=1,  # a proof must fit one message; never silently chunk
            )
            receipt = transaction.execute(
                self._client,
                timeout=self._hs.hcs_submit_timeout_seconds,
                wait_for_receipt=True,
                validate_status=True,  # default is False, which would hide failed consensus statuses
            )
            if receipt.status != ResponseCode.SUCCESS:
                raise HcsError(f"consensus status {getattr(receipt.status, 'name', receipt.status)}")
            sequence = receipt.topic_sequence_number
            if not isinstance(sequence, int) or sequence <= 0:
                raise HcsError("receipt carried no topic sequence number")
            return HcsReceipt(topic_id=self.topic_id, sequence_number=sequence)

    # -- async facade -------------------------------------------------------------------
    async def submit(self, message: bytes) -> HcsReceipt:
        if len(message) > HCS_MAX_MESSAGE_BYTES:
            raise HcsError(f"message is {len(message)} bytes; limit is {HCS_MAX_MESSAGE_BYTES}")
        # Outer timeout is a safety net slightly above the SDK's own deadline, so the SDK normally gives up first.
        outer = self._hs.hcs_submit_timeout_seconds + 5
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._submit_sync, message), timeout=outer)
        except HcsError:
            raise
        except asyncio.TimeoutError as exc:
            raise HcsError(f"no consensus within {outer:.0f}s") from exc
        except Exception as exc:  # SDK raises many types (PrecheckError, MaxAttemptsError, ReceiptStatusError, grpc...)
            raise HcsError(f"{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Durable outbox (SQLite, same database file as the payment ledger)
# ---------------------------------------------------------------------------

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS hcs_outbox (
    channel_id      TEXT    NOT NULL,
    nonce           INTEGER NOT NULL,
    proof_json      TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('pending', 'confirmed', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL    NOT NULL,
    topic_id        TEXT,
    sequence_number INTEGER,
    last_error      TEXT,
    created_at      INTEGER NOT NULL,
    confirmed_at    INTEGER,
    PRIMARY KEY (channel_id, nonce)
);
CREATE INDEX IF NOT EXISTS hcs_outbox_due ON hcs_outbox (status, next_attempt_at);
"""


class Outbox:
    def __init__(self, path: str) -> None:
        self._path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_OUTBOX_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, channel_id: str, nonce: int, proof_json: str, now: float) -> sqlite3.Row:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO hcs_outbox (channel_id, nonce, proof_json, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (channel_id, nonce, proof_json, now, int(now)),
            )
            return conn.execute(
                "SELECT * FROM hcs_outbox WHERE channel_id = ? AND nonce = ?", (channel_id, nonce)
            ).fetchone()
        finally:
            conn.close()

    def due(self, now: float, limit: int = 10) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM hcs_outbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY created_at LIMIT ?",
                (now, limit),
            ).fetchall()
        finally:
            conn.close()

    def seconds_until_next(self, now: float) -> float | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT MIN(next_attempt_at) AS t FROM hcs_outbox WHERE status = 'pending'").fetchone()
        finally:
            conn.close()
        return None if row is None or row["t"] is None else max(0.0, float(row["t"]) - now)

    def mark_confirmed(self, channel_id: str, nonce: int, topic_id: str, sequence_number: int, now: float) -> None:
        self._update(
            "UPDATE hcs_outbox SET status='confirmed', topic_id=?, sequence_number=?, confirmed_at=?, last_error=NULL, "
            "attempts = attempts + 1 WHERE channel_id=? AND nonce=?",
            (topic_id, sequence_number, int(now), channel_id, nonce),
        )

    def mark_retry(self, channel_id: str, nonce: int, next_attempt_at: float, error: str) -> None:
        self._update(
            "UPDATE hcs_outbox SET attempts = attempts + 1, next_attempt_at=?, last_error=? WHERE channel_id=? AND nonce=?",
            (next_attempt_at, error[:500], channel_id, nonce),
        )

    def mark_failed(self, channel_id: str, nonce: int, error: str) -> None:
        self._update(
            "UPDATE hcs_outbox SET status='failed', attempts = attempts + 1, last_error=? WHERE channel_id=? AND nonce=?",
            (error[:500], channel_id, nonce),
        )

    def _update(self, sql: str, params: tuple) -> None:
        conn = self._connect()
        try:
            conn.execute(sql, params)
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM hcs_outbox GROUP BY status").fetchall()
        finally:
            conn.close()
        out = {"pending": 0, "confirmed": 0, "failed": 0}
        out.update({r["status"]: int(r["n"]) for r in rows})
        return out


# ---------------------------------------------------------------------------
# Publisher: the single owner of Hedera submissions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HcsOutcome:
    status: Literal["confirmed", "pending", "failed"]
    topic_id: str | None = None
    sequence_number: int | None = None


OutcomeCallback = Callable[[str, int, HcsOutcome], Awaitable[None]]


class HcsPublisher:
    def __init__(
        self,
        logger: AuditLogger,
        db_path: str,
        *,
        wait_seconds: float = 8.0,
        max_attempts: int = 12,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 300.0,
        poll_seconds: float = 5.0,
        on_outcome: OutcomeCallback | None = None,
    ) -> None:
        self._logger = logger
        self._outbox = Outbox(db_path)
        self.topic_id = logger.topic_id
        self.wait_seconds = wait_seconds
        self.max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._poll = poll_seconds
        self.on_outcome = on_outcome  # set by the app: patches the stored response when HCS settles
        self._waiters: dict[tuple[str, int], list[asyncio.Future[HcsOutcome]]] = {}
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._wake = asyncio.Event()
        pending = (await asyncio.to_thread(self._outbox.counts))["pending"]
        if pending:
            log.info("HCS outbox: resuming %d pending proof(s) from a previous run", pending)
        self._task = asyncio.create_task(self._run(), name="hcs-publisher")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            if self._wake is not None:
                self._wake.set()
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._logger.aclose()

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._outbox.counts)

    # -- request path ---------------------------------------------------------------------
    async def publish(self, channel_id: str, nonce: int, proof_json: str) -> HcsOutcome:
        """Queue the proof and wait (bounded) for consensus.  Never raises on Hedera trouble."""
        key = (channel_id, nonce)
        future: asyncio.Future[HcsOutcome] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(future)  # register BEFORE enqueue: no lost-wakeup race
        try:
            row = await asyncio.to_thread(self._outbox.enqueue, channel_id, nonce, proof_json, time.time())
            if row["status"] == "confirmed":
                return HcsOutcome("confirmed", row["topic_id"], int(row["sequence_number"]))
            if row["status"] == "failed":
                return HcsOutcome("failed", self.topic_id, None)
            if self._wake is not None:
                self._wake.set()
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=self.wait_seconds)
            except asyncio.TimeoutError:
                return HcsOutcome("pending", self.topic_id, None)
        finally:
            waiters = self._waiters.get(key, [])
            if future in waiters:
                waiters.remove(future)
            if not waiters:
                self._waiters.pop(key, None)

    # -- background worker -------------------------------------------------------------------
    async def _run(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            try:
                # Clear BEFORE querying: an enqueue landing after the query re-sets the event, so no wakeup is lost.
                self._wake.clear()
                due = await asyncio.to_thread(self._outbox.due, time.time(), 10)
                if not due:
                    delay = await asyncio.to_thread(self._outbox.seconds_until_next, time.time())
                    timeout = self._poll if delay is None else min(self._poll, max(0.05, delay))
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    continue
                for row in due:
                    if self._stopping:
                        return
                    await self._attempt(row)
            except asyncio.CancelledError:
                raise
            except Exception:  # the worker must never die
                log.exception("HCS worker iteration failed; continuing")
                await asyncio.sleep(1.0)

    def _backoff(self, attempts: int) -> float:
        delay = min(self._retry_max, self._retry_base * (2 ** max(0, attempts - 1)))
        return delay * random.uniform(0.8, 1.2)

    async def _attempt(self, row: sqlite3.Row) -> None:
        channel_id, nonce = row["channel_id"], int(row["nonce"])
        attempts_so_far = int(row["attempts"])
        try:
            receipt = await self._logger.submit(row["proof_json"].encode("utf-8"))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if attempts_so_far + 1 >= self.max_attempts:
                log.error("HCS giving up on channel=%s nonce=%s after %d attempts: %s", channel_id, nonce, attempts_so_far + 1, message)
                await asyncio.to_thread(self._outbox.mark_failed, channel_id, nonce, message)
                await self._settle(channel_id, nonce, HcsOutcome("failed", self.topic_id, None))
            else:
                delay = self._backoff(attempts_so_far + 1)
                log.warning("HCS attempt %d failed for channel=%s nonce=%s (retry in %.1fs): %s", attempts_so_far + 1, channel_id, nonce, delay, message)
                await asyncio.to_thread(self._outbox.mark_retry, channel_id, nonce, time.time() + delay, message)
                self._resolve(channel_id, nonce, HcsOutcome("pending", self.topic_id, None))  # release waiting requests now
            return

        await asyncio.to_thread(
            self._outbox.mark_confirmed, channel_id, nonce, receipt.topic_id, receipt.sequence_number, time.time()
        )
        log.info("HCS confirmed channel=%s nonce=%s topic=%s seq=%d", channel_id, nonce, receipt.topic_id, receipt.sequence_number)
        await self._settle(channel_id, nonce, HcsOutcome("confirmed", receipt.topic_id, receipt.sequence_number))

    async def _settle(self, channel_id: str, nonce: int, outcome: HcsOutcome) -> None:
        if self.on_outcome is not None:
            try:
                await self.on_outcome(channel_id, nonce, outcome)
            except Exception:
                log.exception("HCS outcome callback failed for channel=%s nonce=%s", channel_id, nonce)
        self._resolve(channel_id, nonce, outcome)

    def _resolve(self, channel_id: str, nonce: int, outcome: HcsOutcome) -> None:
        for future in self._waiters.get((channel_id, nonce), []):
            if not future.done():
                future.set_result(outcome)


def build_publisher_from_env(db_path: str) -> HcsPublisher | None:
    """Return a publisher if Hedera is configured, None if it is wholly unconfigured; raise if half-configured."""
    hs = HederaSettings()
    if not hs.has_operator and not hs.has_topic:
        log.info("HCS audit logging disabled (no OPERATOR_ID / OPERATOR_KEY / TOPIC_ID configured)")
        return None
    if not (hs.has_operator and hs.has_topic):
        raise RuntimeError(
            "Hedera is half-configured: set all of OPERATOR_ID, OPERATOR_KEY, TOPIC_ID "
            "(run `python scripts/create_topic.py` to obtain a TOPIC_ID) or none of them."
        )
    return HcsPublisher(
        HederaAuditLogger(hs),
        db_path,
        wait_seconds=hs.hcs_wait_seconds,
        max_attempts=hs.hcs_max_attempts,
        retry_base_seconds=hs.hcs_retry_base_seconds,
        retry_max_seconds=hs.hcs_retry_max_seconds,
    )