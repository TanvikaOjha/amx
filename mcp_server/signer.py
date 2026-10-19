"""Voucher signing and spend limits for the AMX MCP server (protocol section 3, payer side).

THREAT MODEL - the gateway is UNTRUSTED input to this module.
  A buggy or malicious gateway can put any numbers in its 402 challenge.  A voucher is claimable on-chain
  up to the channel deposit, so signing "whatever the challenge says" would let a hostile gateway drain
  the channel.  Therefore:

  * Spend is measured against this signer's OWN persisted record of what it has signed (STATE_FILE),
    never against amounts the server reports.
  * A call may raise the signed cumulative by at most the (capped) price; if the server claims we are
    further ahead than our own record, we refuse unless the operator explicitly allowed a resync, and even
    then the jump counts against MAX_SESSION_SPEND.
  * The EIP-712 domain (chain id, verifying contract) comes from LOCAL configuration, never from the challenge.
  * Testnets only: configuration with any other chain id is rejected, because this process holds a key.

Lifecycle of one payment:  prepare()  ->  (send ticket)  ->  commit() | abort()
  prepare() checks every limit, signs, and PERSISTS the advanced state before the ticket is ever sent, so a
  crash can cost at most one price unit of over-payment and can never cause a nonce to be reused.
  abort() is for outcomes where the gateway has definitively NOT charged us; it restores the previous state.

Protocol helpers (canonical JSON, EIP-712, ticket encoding) are intentionally a standalone copy of
gateway/app/canonical.py so this package installs without the gateway; tests/test_gateway_parity.py proves the
two implementations agree byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_keys import keys as eth_keys
from eth_utils import is_address, keccak, to_checksum_address
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent / ".env"  # absolute: Claude Desktop / Cursor start us with an unknown cwd

MAX_SAFE_INT = 2**53 - 1
UINT256_MAX = 2**256 - 1
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
TESTNET_CHAIN_IDS = {84532: "base-sepolia", 11155111: "sepolia", 31337: "local hardhat"}
TICKET_HEADER = "X-402-Payment-Ticket"
CHANNEL_HINT_HEADER = "X-AMX-Channel-Id"

_BYTES32_RE = re.compile(r"^0x[0-9a-f]{64}$")
_UINT_RE = re.compile(r"^(0|[1-9][0-9]{0,77})$")


# ---------------------------------------------------------------------------
# Errors (messages are shown to the model / user: they must never contain secrets)
# ---------------------------------------------------------------------------


class SignerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SignerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    gateway_url: str = "http://localhost:8000"
    payer_private_key: SecretStr
    channel_id: str
    agent_id: int = Field(ge=1, le=MAX_SAFE_INT)
    chain_id: int
    channel_contract: str
    chain_network: str = "base-sepolia"

    # Budget: both REQUIRED. There is deliberately no default: the operator must pick a number.
    max_price_per_call: int = Field(ge=1)  # wei, upper bound on the gateway's advertised pricePerCall
    max_session_spend: int = Field(ge=1)  # wei, upper bound on total signed increase per server process

    voucher_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    request_timeout_seconds: float = Field(default=45.0, gt=0)  # includes the gateway's HCS wait (8s default)
    state_file: Path = ENV_FILE.parent / "amx_signer_state.json"
    allow_resync: bool = False
    allow_insecure_http: bool = False
    hedera_network: Literal["testnet", "previewnet"] = "testnet"  # only used to build a HashScan link

    @field_validator("channel_contract")
    @classmethod
    def _checksum(cls, value: str) -> str:
        if not is_address(value):
            raise ValueError("must be a 20-byte hex address")
        return to_checksum_address(value)

    @field_validator("channel_id")
    @classmethod
    def _channel_id(cls, value: str) -> str:
        value = value.strip()
        if not _BYTES32_RE.match(value):
            raise ValueError("must be 0x + 64 lowercase hex characters")
        return value

    @field_validator("payer_private_key")
    @classmethod
    def _key_shape(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        raw = raw[2:] if raw.startswith("0x") else raw
        try:
            if len(raw) != 64:
                raise ValueError
            bytes.fromhex(raw)
        except ValueError:
            raise ValueError("must be a 32-byte hex private key") from None  # never echo the value
        return SecretStr("0x" + raw)

    @field_validator("chain_id")
    @classmethod
    def _testnet_only(cls, value: int) -> int:
        if value not in TESTNET_CHAIN_IDS:
            raise ValueError(
                f"chain id {value} is not an allowed testnet {sorted(TESTNET_CHAIN_IDS)}; "
                "this project is testnet-only and this process holds a private key"
            )
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> "SignerSettings":
        if self.max_session_spend < self.max_price_per_call:
            raise ValueError("MAX_SESSION_SPEND must be at least MAX_PRICE_PER_CALL")
        parsed = urlparse(self.gateway_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("GATEWAY_URL must be an http(s) URL")
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme == "http" and not local and not self.allow_insecure_http:
            raise ValueError("GATEWAY_URL must be https unless it points at localhost (or ALLOW_INSECURE_HTTP=true)")
        self.gateway_url = self.gateway_url.rstrip("/")
        return self

    @property
    def payer_address(self) -> str:
        return Account.from_key(self.payer_private_key.get_secret_value()).address


# ---------------------------------------------------------------------------
# Canonical JSON (identical rules to gateway/app/canonical.py)
# ---------------------------------------------------------------------------


class CanonicalizationError(ValueError):
    pass


def _validate_canonical(obj: Any, depth: int = 0) -> None:
    if depth > 32:
        raise CanonicalizationError("nesting too deep")
    if obj is None or isinstance(obj, (bool, str)):
        return
    if isinstance(obj, int):
        if abs(obj) > MAX_SAFE_INT:
            raise CanonicalizationError("integer outside the +/-(2**53-1) safe range")
        return
    if isinstance(obj, float):
        raise CanonicalizationError("floats are not allowed in canonical JSON")
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _validate_canonical(item, depth + 1)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
            _validate_canonical(value, depth + 1)
        return
    raise CanonicalizationError(f"unsupported type: {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    _validate_canonical(obj)
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("string is not valid UTF-8") from exc


def request_hash(body: Any) -> str:
    """0x + sha256(canonical body): what a voucher's requestHash must equal."""
    return "0x" + hashlib.sha256(canonical_bytes(body)).hexdigest()


# ---------------------------------------------------------------------------
# EIP-712 voucher (must match PaymentChannel.sol and gateway/app/canonical.py)
# ---------------------------------------------------------------------------

DOMAIN_NAME = "AMXPaymentChannel"
DOMAIN_VERSION = "1"


def voucher_typed_data(chain_id: int, channel_contract: str, voucher: dict[str, Any]) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Voucher": [
                {"name": "channelId", "type": "bytes32"},
                {"name": "agentId", "type": "uint256"},
                {"name": "cumulativeAmount", "type": "uint256"},
                {"name": "nonce", "type": "uint64"},
                {"name": "expiry", "type": "uint64"},
                {"name": "requestHash", "type": "bytes32"},
            ],
        },
        "primaryType": "Voucher",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(channel_contract),
        },
        "message": {
            "channelId": voucher["channelId"],
            "agentId": int(voucher["agentId"]),
            "cumulativeAmount": int(voucher["cumulativeAmount"]),
            "nonce": int(voucher["nonce"]),
            "expiry": int(voucher["expiry"]),
            "requestHash": voucher["requestHash"],
        },
    }


def voucher_digest(chain_id: int, channel_contract: str, voucher: dict[str, Any]) -> bytes:
    """Independent re-implementation of the EIP-712 digest, used to cross-check eth_account's signer."""
    typehash = keccak(
        text="Voucher(bytes32 channelId,uint256 agentId,uint256 cumulativeAmount,uint64 nonce,uint64 expiry,bytes32 requestHash)"
    )
    domain_typehash = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    domain = keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [domain_typehash, keccak(text=DOMAIN_NAME), keccak(text=DOMAIN_VERSION), chain_id, to_checksum_address(channel_contract)],
        )
    )
    struct = keccak(
        abi_encode(
            ["bytes32", "bytes32", "uint256", "uint256", "uint64", "uint64", "bytes32"],
            [
                typehash,
                bytes.fromhex(voucher["channelId"][2:]),
                int(voucher["agentId"]),
                int(voucher["cumulativeAmount"]),
                int(voucher["nonce"]),
                int(voucher["expiry"]),
                bytes.fromhex(voucher["requestHash"][2:]),
            ],
        )
    )
    return keccak(b"\x19\x01" + domain + struct)


def encode_ticket(voucher: dict[str, Any], signature: str) -> str:
    """Header value: base64url(JSON{voucher, signature}), unpadded."""
    payload = json.dumps({"voucher": voucher, "signature": signature}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# The 402 challenge, parsed defensively
# ---------------------------------------------------------------------------


def _parse_uint(value: Any, field: str) -> int:
    if not isinstance(value, str) or not _UINT_RE.match(value) or int(value) > UINT256_MAX:
        raise SignerError("malformed_challenge", f"challenge field '{field}' is not a valid uint256 decimal string")
    return int(value)


@dataclass(frozen=True)
class Accept:
    price: int
    min_nonce: int
    min_cumulative: int


class _AcceptModel(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate fields added by future gateway versions

    scheme: str
    network: str
    channelContract: str
    agentId: int
    pricePerCall: str
    asset: str
    minNonce: int
    minCumulativeAmount: str


def select_accept(challenge: Any, settings: SignerSettings) -> Accept:
    """Pick the one acceptable payment option from an untrusted challenge, or refuse with a precise reason."""
    if not isinstance(challenge, dict) or challenge.get("error") != "payment_required":
        raise SignerError("malformed_challenge", "gateway returned a 402 that is not an AMX payment challenge")
    options = challenge.get("accepts")
    if not isinstance(options, list) or not options:
        raise SignerError("malformed_challenge", "challenge offers no payment options")

    mismatches: list[str] = []
    for raw in options:
        try:
            option = _AcceptModel.model_validate(raw)
        except ValidationError:
            mismatches.append("an option is malformed")
            continue

        problems: list[str] = []
        if option.scheme != "amx-voucher":
            problems.append(f"scheme '{option.scheme}'")
        if option.asset != "native":
            problems.append(f"asset '{option.asset}'")
        if option.network != settings.chain_network:
            problems.append(f"network '{option.network}' (expected '{settings.chain_network}')")
        if option.agentId != settings.agent_id:
            problems.append(f"agentId {option.agentId} (expected {settings.agent_id})")
        if not is_address(option.channelContract) or to_checksum_address(option.channelContract) != settings.channel_contract:
            problems.append("channelContract differs from the configured contract")
        if problems:
            mismatches.append(", ".join(problems))
            continue

        if option.minNonce < 1 or option.minNonce > MAX_SAFE_INT:
            raise SignerError("malformed_challenge", "challenge minNonce is out of range")
        return Accept(
            price=_parse_uint(option.pricePerCall, "pricePerCall"),
            min_nonce=option.minNonce,
            min_cumulative=_parse_uint(option.minCumulativeAmount, "minCumulativeAmount"),
        )

    raise SignerError("challenge_mismatch", "no acceptable payment option: " + "; ".join(mismatches))


# ---------------------------------------------------------------------------
# Persistent signer state (what WE have signed)
# ---------------------------------------------------------------------------


class SignerState:
    """JSON file {version, channels: {channelId: {last_nonce, last_cumulative}}}, written atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, channel_id: str) -> tuple[int, int]:
        if not self._path.exists():
            return 0, 0
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            entry = data["channels"].get(channel_id)
            if entry is None:
                return 0, 0
            nonce, cumulative = int(entry["last_nonce"]), int(entry["last_cumulative"])
            if nonce < 0 or cumulative < 0:
                raise ValueError
            return nonce, cumulative
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # Do NOT silently reset: forgetting what we signed would break the spend accounting.
            raise SignerError(
                "state_corrupt",
                f"signer state file {self._path} is unreadable; fix or delete it deliberately "
                "(deleting it requires ALLOW_RESYNC=true for a channel that has already been used)",
            ) from None

    def save(self, channel_id: str, nonce: int, cumulative: int) -> None:
        data: dict[str, Any] = {"version": 1, "channels": {}}
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded.get("channels"), dict):
                    data = loaded
            except (OSError, ValueError):
                raise SignerError("state_corrupt", f"signer state file {self._path} is unreadable") from None
        data["channels"][channel_id] = {"last_nonce": nonce, "last_cumulative": str(cumulative)}

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".signer-state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:  # e.g. Windows
                pass
            os.replace(tmp, self._path)  # atomic
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# The signer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingVoucher:
    voucher: dict[str, Any]
    signature: str
    ticket: str  # ready-to-send header value
    nonce: int
    cumulative: int
    delta: int  # how much the signed cumulative rose: what this call can cost
    price: int
    prev_nonce: int
    prev_cumulative: int
    request_hash: str


class VoucherSigner:
    def __init__(self, settings: SignerSettings) -> None:
        self._s = settings
        self._key = settings.payer_private_key.get_secret_value()
        self._state = SignerState(settings.state_file)
        self._session_spent = 0
        self._inflight: PendingVoucher | None = None
        self.address = settings.payer_address

    # -- budget view ----------------------------------------------------------
    @property
    def session_spent(self) -> int:
        return self._session_spent

    @property
    def session_remaining(self) -> int:
        return max(0, self._s.max_session_spend - self._session_spent)

    # -- lifecycle ------------------------------------------------------------
    def prepare(self, accept: Accept, req_hash: str, *, now: int | None = None) -> PendingVoucher:
        """Check every limit, sign, and persist the advanced state.  Raises SignerError without side effects."""
        if self._inflight is not None:
            raise SignerError("busy", "another payment is still being settled")
        if not _BYTES32_RE.match(req_hash):
            raise SignerError("bad_request_hash", "request hash must be 0x + 64 lowercase hex characters")

        if accept.price > self._s.max_price_per_call:
            raise SignerError(
                "price_exceeds_cap",
                f"gateway price {accept.price} wei exceeds MAX_PRICE_PER_CALL ({self._s.max_price_per_call}); nothing was signed",
            )

        last_nonce, last_cumulative = self._state.load(self._s.channel_id)
        nonce = max(accept.min_nonce, last_nonce + 1)
        cumulative = max(accept.min_cumulative, last_cumulative + accept.price)

        server_ahead = accept.min_nonce > last_nonce + 1 or accept.min_cumulative > last_cumulative + accept.price
        if server_ahead and not self._s.allow_resync:
            raise SignerError(
                "state_ahead_of_signer",
                f"the gateway says this channel is at nonce {accept.min_nonce - 1} / cumulative "
                f"{accept.min_cumulative - accept.price}, ahead of this signer's own record "
                f"(nonce {last_nonce} / cumulative {last_cumulative}). This happens if the state file was lost or "
                "another client uses the channel - or if the gateway is lying. Nothing was signed. "
                "If you trust it, set ALLOW_RESYNC=true (the jump still counts against MAX_SESSION_SPEND).",
            )
        if nonce > MAX_SAFE_INT or cumulative > UINT256_MAX:
            raise SignerError("amount_out_of_range", "nonce or cumulative amount is out of range")

        delta = cumulative - last_cumulative
        if self._session_spent + delta > self._s.max_session_spend:
            raise SignerError(
                "session_cap_exceeded",
                f"this call would raise session spend to {self._session_spent + delta} wei, above "
                f"MAX_SESSION_SPEND ({self._s.max_session_spend}); nothing was signed",
            )

        voucher = {
            "channelId": self._s.channel_id,
            "agentId": self._s.agent_id,
            "cumulativeAmount": str(cumulative),
            "nonce": nonce,
            "expiry": (now if now is not None else int(time.time())) + self._s.voucher_ttl_seconds,
            "requestHash": req_hash,
        }
        message = voucher_typed_data(self._s.chain_id, self._s.channel_contract, voucher)
        signed = Account.sign_typed_data(self._key, full_message=message)
        signature = "0x" + bytes(signed.signature).hex()
        self._assert_acceptable_signature(signature, voucher)

        pending = PendingVoucher(
            voucher=voucher,
            signature=signature,
            ticket=encode_ticket(voucher, signature),
            nonce=nonce,
            cumulative=cumulative,
            delta=delta,
            price=accept.price,
            prev_nonce=last_nonce,
            prev_cumulative=last_cumulative,
            request_hash=req_hash,
        )
        self._state.save(self._s.channel_id, nonce, cumulative)  # persist BEFORE the ticket can leave this process
        self._session_spent += delta
        self._inflight = pending
        return pending

    def commit(self, pending: PendingVoucher) -> None:
        """The voucher has been (or must be assumed to have been) spent. State and session spend stay advanced."""
        if self._inflight is pending:
            self._inflight = None

    def abort(self, pending: PendingVoucher) -> None:
        """The gateway definitively did NOT charge for this voucher: restore the previous state."""
        if self._inflight is not pending:
            return
        current = self._state.load(self._s.channel_id)
        if current == (pending.nonce, pending.cumulative):  # only roll back if nothing newer was signed
            self._state.save(self._s.channel_id, pending.prev_nonce, pending.prev_cumulative)
        self._session_spent = max(0, self._session_spent - pending.delta)
        self._inflight = None

    # -- internals -----------------------------------------------------------------
    def _assert_acceptable_signature(self, signature: str, voucher: dict[str, Any]) -> None:
        """Belt and braces: the gateway (and the contract) reject high-s or mis-recovering signatures."""
        raw = bytes.fromhex(signature[2:])
        s = int.from_bytes(raw[32:64], "big")
        if len(raw) != 65 or raw[64] not in (27, 28) or s > SECP256K1_N // 2:
            raise SignerError("bad_signature", "internal error: produced a non-canonical signature")
        digest = voucher_digest(self._s.chain_id, self._s.channel_contract, voucher)
        r = int.from_bytes(raw[0:32], "big")
        try:
            recovered = eth_keys.Signature(vrs=(raw[64] - 27, r, s)).recover_public_key_from_msg_hash(digest).to_checksum_address()
        except Exception:
            recovered = ""
        if recovered != self.address:
            raise SignerError("bad_signature", "internal error: signature does not recover to the payer address")