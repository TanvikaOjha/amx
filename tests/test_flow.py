"""Phase 5 - end-to-end flow test against a RUNNING gateway (real chain, real Hedera testnet).

Start the gateway first (see README / the commands in the Phase 5 notes), then:

    pip install -r tests/requirements.txt
    pytest tests/test_flow.py -v

What it proves, in order:
  1. POST without a ticket          -> 402 with a valid amx-voucher challenge
  2. Build + sign a voucher from the challenge (EIP-712, recovered and checked locally)
  3. Retry with the ticket          -> 200, HCS sequence number, and the message is found on the
                                       Hedera mirror node REST API with a valid gatewaySig
  4. Replay the same voucher        -> idempotent original result, nothing charged twice
                                       (+ a ticket cannot pay for a different body, nor reuse a nonce)
  5. Voucher with a lower nonce     -> 402 nonce_not_increasing

The test builds hashes and signatures with its OWN small implementation (not gateway/ or mcp_server/ code),
so it is a third, independent check that the protocol is implemented consistently.
Testnets only.  The payer key is read from tests/.env and is never printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import is_address, to_checksum_address

load_dotenv(Path(__file__).resolve().parent / ".env")  # real environment variables win over the file

TESTNET_NETWORKS = {84532: "base-sepolia", 11155111: "sepolia", 31337: "local"}
MIRROR_BASE_URLS = {
    "testnet": "https://testnet.mirrornode.hedera.com",
    "previewnet": "https://previewnet.mirrornode.hedera.com",
}
HCS_MAX_MESSAGE_BYTES = 1024
PROOF_FIELDS = {"agentId", "channelId", "nonce", "requestHash", "responseHash", "ts", "gatewaySig"}
TICKET_HEADER = "X-402-Payment-Ticket"
CHANNEL_HINT_HEADER = "X-AMX-Channel-Id"
_BYTES32 = re.compile(r"^0x[0-9a-f]{64}$")
_TOPIC = re.compile(r"^\d+\.\d+\.\d+$")


# ---------------------------------------------------------------------------
# Protocol helpers (independent of the code under test)
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8 (protocol section 3)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return "0x" + hashlib.sha256(data).hexdigest()


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
            "name": "AMXPaymentChannel",
            "version": "1",
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


def recover_proof_signer(proof: dict[str, Any]) -> str:
    """gatewaySig = EIP-191 signature over sha256(canonical JSON of the other fields)."""
    unsigned = {k: v for k, v in proof.items() if k != "gatewaySig"}
    digest = hashlib.sha256(canonical_bytes(unsigned)).digest()
    return Account.recover_message(encode_defunct(primitive=digest), signature=proof["gatewaySig"])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    gateway_url: str
    payer_key: str = field(repr=False)  # never shown in assertion output
    channel_id: str
    chain_id: int
    channel_contract: str
    hedera_network: str
    hcs_wait_seconds: float
    mirror_wait_seconds: float
    expected_signer: str | None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_config() -> Config:
    required = ("PAYER_PRIVATE_KEY", "CHANNEL_ID", "CHAIN_ID", "CHANNEL_CONTRACT")
    missing = [name for name in required if not _env(name)]
    if missing:
        pytest.fail(
            "Missing configuration: " + ", ".join(missing) + ". Copy tests/.env.example to tests/.env and fill it in.",
            pytrace=False,
        )

    problems: list[str] = []
    key = _env("PAYER_PRIVATE_KEY")
    raw = key[2:] if key.startswith("0x") else key
    try:
        if len(raw) != 64:
            raise ValueError
        bytes.fromhex(raw)
    except ValueError:
        problems.append("PAYER_PRIVATE_KEY must be a 32-byte hex key")  # never echo the value
    if not _BYTES32.match(_env("CHANNEL_ID")):
        problems.append("CHANNEL_ID must be 0x + 64 lowercase hex characters")
    if not is_address(_env("CHANNEL_CONTRACT")):
        problems.append("CHANNEL_CONTRACT must be a 20-byte hex address")
    chain_id = 0
    try:
        chain_id = int(_env("CHAIN_ID"))
    except ValueError:
        problems.append("CHAIN_ID must be an integer")
    if chain_id and chain_id not in TESTNET_NETWORKS:
        problems.append(f"CHAIN_ID {chain_id} is not an allowed testnet {sorted(TESTNET_NETWORKS)} (testnets only)")
    network = _env("HEDERA_NETWORK", "testnet")
    if network not in MIRROR_BASE_URLS:
        problems.append("HEDERA_NETWORK must be testnet or previewnet")
    expected = _env("EXPECTED_AGENT_SIGNER") or None
    if expected and not is_address(expected):
        problems.append("EXPECTED_AGENT_SIGNER must be a 20-byte hex address")
    if problems:
        pytest.fail("Invalid configuration:\n  - " + "\n  - ".join(problems), pytrace=False)

    return Config(
        gateway_url=_env("GATEWAY_URL", "http://localhost:8000").rstrip("/"),
        payer_key="0x" + raw,
        channel_id=_env("CHANNEL_ID"),
        chain_id=chain_id,
        channel_contract=to_checksum_address(_env("CHANNEL_CONTRACT")),
        hedera_network=network,
        hcs_wait_seconds=float(_env("HCS_WAIT_SECONDS", "90")),
        mirror_wait_seconds=float(_env("MIRROR_WAIT_SECONDS", "60")),
        expected_signer=to_checksum_address(expected) if expected else None,
    )


# ---------------------------------------------------------------------------
# The rig: everything a payer client does, written out explicitly
# ---------------------------------------------------------------------------


@dataclass
class Paid:
    body: dict[str, Any]
    raw: bytes
    voucher: dict[str, Any]
    signature: str
    ticket: str
    response: httpx.Response
    price: int | None = None
    accept: dict[str, Any] | None = None


class Rig:
    def __init__(self, cfg: Config, http: httpx.Client) -> None:
        self.cfg = cfg
        self.http = http
        self.payer = Account.from_key(cfg.payer_key)

    # -- requests ----------------------------------------------------------------
    @staticmethod
    def new_body(label: str) -> dict[str, Any]:
        """A unique job each time, so a re-run never collides with an earlier run's idempotency record."""
        return {"task": "sha256", "input": f"amx-e2e {label} {uuid.uuid4().hex}"}

    @staticmethod
    def wire(body: dict[str, Any]) -> bytes:
        """Deliberately NOT canonical (indented, insertion order): the hash must not depend on wire formatting."""
        return json.dumps(body, indent=2).encode("utf-8")

    def post(self, raw: bytes, ticket: str | None = None, *, hint: bool = True) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if ticket is not None:
            headers[TICKET_HEADER] = ticket
        if hint:
            headers[CHANNEL_HINT_HEADER] = self.cfg.channel_id
        return self.http.post("/v1/execute", content=raw, headers=headers)

    def channel_state(self) -> dict[str, Any]:
        r = self.http.get(f"/v1/channels/{self.cfg.channel_id}")
        assert r.status_code == 200, r.text
        return r.json()

    # -- challenge ---------------------------------------------------------------
    @staticmethod
    def challenge_accept(response: httpx.Response) -> dict[str, Any]:
        assert response.status_code == 402, f"expected 402, got {response.status_code}: {response.text[:300]}"
        body = response.json()
        assert body.get("error") == "payment_required"
        accepts = body.get("accepts")
        assert isinstance(accepts, list) and accepts, "challenge has no accepts"
        return accepts[0]

    # -- voucher -----------------------------------------------------------------
    def voucher(self, body: dict[str, Any], nonce: int, cumulative: int, expiry: int | None = None) -> dict[str, Any]:
        return {
            "channelId": self.cfg.channel_id,
            "agentId": self.agent_id,
            "cumulativeAmount": str(cumulative),
            "nonce": nonce,
            "expiry": expiry if expiry is not None else int(time.time()) + 300,
            "requestHash": sha256_hex(canonical_bytes(body)),
        }

    agent_id: int = 0  # set from the first challenge

    def sign(self, voucher: dict[str, Any]) -> str:
        message = voucher_typed_data(self.cfg.chain_id, self.cfg.channel_contract, voucher)
        signed = Account.sign_typed_data(self.cfg.payer_key, full_message=message)
        return "0x" + bytes(signed.signature).hex()

    @staticmethod
    def header(voucher: dict[str, Any], signature: str) -> str:
        payload = json.dumps({"voucher": voucher, "signature": signature}, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")  # base64url, unpadded

    def send_voucher(
        self, body: dict[str, Any], nonce: int, cumulative: int, *, price: int | None = None, accept: dict[str, Any] | None = None
    ) -> Paid:
        if accept is not None:
            self.agent_id = int(accept["agentId"])
        voucher = self.voucher(body, nonce, cumulative)
        signature = self.sign(voucher)
        ticket = self.header(voucher, signature)
        raw = self.wire(body)
        return Paid(body, raw, voucher, signature, ticket, self.post(raw, ticket), price, accept)

    def pay(self, body: dict[str, Any]) -> Paid:
        """Probe for the challenge, then pay exactly what it asks for."""
        accept = self.challenge_accept(self.post(b"{}"))
        return self.send_voucher(
            body, int(accept["minNonce"]), int(accept["minCumulativeAmount"]), price=int(accept["pricePerCall"]), accept=accept
        )


# ---------------------------------------------------------------------------
# Fixtures (module scope: one paid job feeds several assertions, so each step shows up as its own test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


@pytest.fixture(scope="module")
def http(cfg: Config):
    with httpx.Client(base_url=cfg.gateway_url, timeout=30.0, follow_redirects=False) as client:
        try:
            health = client.get("/health")
        except httpx.HTTPError as exc:
            pytest.fail(
                f"Cannot reach the gateway at {cfg.gateway_url} ({type(exc).__name__}). Start it first: "
                "`docker compose up --build` from the repo root, or `uvicorn app.main:create_app --factory --port 8000` from gateway/.",
                pytrace=False,
            )
        data = health.json() if health.status_code == 200 else {}
        if data.get("registry") != "ok":
            pytest.fail(f"Gateway is up but its registry check is {data.get('registry')!r}: {health.text[:300]}", pytrace=False)
        yield client


@pytest.fixture(scope="module")
def rig(cfg: Config, http: httpx.Client) -> Rig:
    return Rig(cfg, http)


@pytest.fixture(scope="module")
def first_paid(rig: Rig) -> Paid:
    paid = rig.pay(rig.new_body("first"))
    if paid.response.status_code != 200:
        pytest.fail(f"the first paid call failed: HTTP {paid.response.status_code} {paid.response.text[:400]}", pytrace=False)
    return paid


@pytest.fixture(scope="module")
def hcs_data(rig: Rig, cfg: Config, first_paid: Paid) -> dict[str, Any]:
    """The job response once HCS is confirmed.  If Hedera is slow the gateway answers 'pending'; the SAME ticket is
    re-sent (idempotent, never charged twice) until the stored response carries the sequence number."""
    data = first_paid.response.json()
    deadline = time.monotonic() + cfg.hcs_wait_seconds
    while True:
        status = data.get("hcs_status")
        if status == "confirmed":
            return data
        if status == "disabled":
            pytest.fail(
                "The gateway reports hcs_status=disabled. Set OPERATOR_ID, OPERATOR_KEY (+OPERATOR_KEY_TYPE) and TOPIC_ID "
                "in gateway/.env (python scripts/create_topic.py) and restart it.",
                pytrace=False,
            )
        if status == "failed":
            pytest.fail("The gateway gave up delivering the HCS proof (hcs_status=failed). Check the gateway log.", pytrace=False)
        if time.monotonic() >= deadline:
            pytest.fail(f"HCS still {status!r} after {cfg.hcs_wait_seconds:.0f}s.", pytrace=False)
        time.sleep(3.0)
        replay = rig.post(first_paid.raw, first_paid.ticket)
        assert replay.status_code == 200, replay.text
        data = replay.json()


@pytest.fixture(scope="module")
def mirror_record(cfg: Config, hcs_data: dict[str, Any]) -> dict[str, Any]:
    """The message as served by the Hedera mirror node REST API (it lags consensus by a few seconds)."""
    url = f"{MIRROR_BASE_URLS[cfg.hedera_network]}/api/v1/topics/{hcs_data['hcs_topic_id']}/messages/{hcs_data['hcs_sequence_number']}"
    deadline = time.monotonic() + cfg.mirror_wait_seconds
    last = "no response"
    with httpx.Client(timeout=15.0) as client:
        while True:
            try:
                response = client.get(url)
                if response.status_code == 200:
                    return response.json()
                last = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            if time.monotonic() >= deadline:
                pytest.fail(f"message not available from the mirror node within {cfg.mirror_wait_seconds:.0f}s ({last}): {url}", pytrace=False)
            time.sleep(3.0)


@pytest.fixture(scope="module")
def agent_card(http: httpx.Client) -> dict[str, Any]:
    r = http.get("/.well-known/agent-card.json")
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# Step 1: no ticket -> 402 + challenge
# ===========================================================================


def test_step1_no_ticket_returns_a_valid_402_challenge(rig: Rig, cfg: Config):
    r = rig.post(rig.wire(rig.new_body("probe")))
    assert r.status_code == 402, r.text
    assert r.headers["www-authenticate"].startswith("amx-voucher")
    challenge = r.json()
    assert challenge["error"] == "payment_required"
    assert challenge.get("reason") is None  # no ticket was presented, so there is nothing to explain
    assert len(challenge["accepts"]) == 1

    accept = challenge["accepts"][0]
    assert accept["scheme"] == "amx-voucher" and accept["asset"] == "native"
    assert accept["network"] == TESTNET_NETWORKS[cfg.chain_id]
    assert to_checksum_address(accept["channelContract"]) == cfg.channel_contract
    assert isinstance(accept["agentId"], int) and accept["agentId"] >= 1
    price = int(accept["pricePerCall"])
    assert price > 0

    # With the channel hint the minimums describe THIS channel's next voucher, per the gateway's own ledger.
    state = rig.channel_state()
    assert accept["minNonce"] == state["lastNonce"] + 1
    assert int(accept["minCumulativeAmount"]) == int(state["lastCumulativeAmount"]) + price

    # Without the hint the gateway cannot know the channel, so it states the baseline.
    bare = rig.challenge_accept(rig.post(rig.wire(rig.new_body("probe")), hint=False))
    assert (bare["minNonce"], int(bare["minCumulativeAmount"])) == (1, price)


# ===========================================================================
# Step 2: build and sign a voucher from the challenge
# ===========================================================================


def test_step2_voucher_is_built_from_the_challenge_and_signed_by_the_payer(rig: Rig, cfg: Config, first_paid: Paid):
    voucher, accept = first_paid.voucher, first_paid.accept
    assert accept is not None
    assert voucher["nonce"] == accept["minNonce"]
    assert voucher["cumulativeAmount"] == accept["minCumulativeAmount"]
    assert voucher["agentId"] == accept["agentId"]
    assert voucher["channelId"] == cfg.channel_id
    assert voucher["requestHash"] == sha256_hex(canonical_bytes(first_paid.body))  # binds the ticket to this one job
    assert voucher["expiry"] > int(time.time())

    signable = encode_typed_data(full_message=voucher_typed_data(cfg.chain_id, cfg.channel_contract, voucher))
    assert Account.recover_message(signable, signature=first_paid.signature) == rig.payer.address

    # PaymentChannel.sol only accepts canonical signatures: 65 bytes, v in {27, 28}, low-s.
    raw = bytes.fromhex(first_paid.signature[2:])
    assert len(raw) == 65 and raw[64] in (27, 28)
    assert int.from_bytes(raw[32:64], "big") <= 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0


# ===========================================================================
# Step 3: retry with the ticket -> 200, HCS sequence number, mirror-node confirmation
# ===========================================================================


def test_step3_retry_with_the_ticket_returns_200_and_a_correct_receipt(first_paid: Paid):
    assert first_paid.response.status_code == 200
    data = first_paid.response.json()
    voucher = first_paid.voucher

    assert data["task"] == "sha256"
    assert data["output"] == hashlib.sha256(first_paid.body["input"].encode()).hexdigest()
    assert data["idempotent_replay"] is False
    assert data["request_hash"] == voucher["requestHash"]
    assert (data["channel_id"], data["nonce"], data["cumulative_amount"]) == (voucher["channelId"], voucher["nonce"], voucher["cumulativeAmount"])
    assert int(data["charged_amount"]) == first_paid.price

    expected_response_hash = sha256_hex(
        canonical_bytes({"job_id": data["job_id"], "task": data["task"], "output": data["output"], "request_hash": data["request_hash"]})
    )
    assert data["response_hash"] == expected_response_hash


def test_step3_response_carries_a_hedera_hcs_sequence_number(hcs_data: dict[str, Any]):
    assert hcs_data["hcs_status"] == "confirmed"
    assert _TOPIC.match(hcs_data["hcs_topic_id"])
    assert isinstance(hcs_data["hcs_sequence_number"], int) and hcs_data["hcs_sequence_number"] > 0


def test_step3_the_message_is_on_the_mirror_node_and_the_proof_is_valid(
    cfg: Config, rig: Rig, first_paid: Paid, hcs_data: dict[str, Any], mirror_record: dict[str, Any], agent_card: dict[str, Any]
):
    assert mirror_record["topic_id"] == hcs_data["hcs_topic_id"]
    assert mirror_record["sequence_number"] == hcs_data["hcs_sequence_number"]

    raw = base64.b64decode(mirror_record["message"], validate=True)
    assert len(raw) <= HCS_MAX_MESSAGE_BYTES
    proof = json.loads(raw)
    assert set(proof) == PROOF_FIELDS
    assert raw == canonical_bytes(proof)  # canonical form, byte for byte

    voucher = first_paid.voucher
    assert proof["agentId"] == voucher["agentId"]
    assert proof["channelId"] == cfg.channel_id
    assert proof["nonce"] == voucher["nonce"]
    assert proof["requestHash"] == hcs_data["request_hash"]
    assert proof["responseHash"] == hcs_data["response_hash"]
    assert abs(proof["ts"] - time.time()) < 600

    # Privacy: no task content on a public topic.
    assert first_paid.body["input"].encode() not in raw
    assert hcs_data["output"].encode() not in raw

    # Authenticity: gatewaySig recovers to the agent's signing key published in the agent card
    # (and, if you supplied it, to the signerOf(agentId) value you looked up yourself).
    signer = to_checksum_address(recover_proof_signer(proof))
    assert signer == to_checksum_address(agent_card["amx"]["signingKey"]["address"])
    assert any(r["agentId"] == proof["agentId"] for r in agent_card["registrations"])
    if cfg.expected_signer:
        assert signer == cfg.expected_signer


# ===========================================================================
# Step 4: replay
# ===========================================================================


def test_step4_replaying_the_same_voucher_returns_the_original_and_charges_nothing(rig: Rig, first_paid: Paid):
    before = rig.channel_state()
    original = first_paid.response.json()

    r = rig.post(first_paid.raw, first_paid.ticket)
    assert r.status_code == 200, r.text
    assert r.headers["x-amx-idempotent-replay"] == "true"
    replay = r.json()
    assert replay["idempotent_replay"] is True
    for key in ("job_id", "output", "request_hash", "response_hash", "nonce", "cumulative_amount"):
        assert replay[key] == original[key], key

    assert rig.channel_state() == before  # not charged a second time


def test_step4_a_ticket_cannot_pay_for_a_different_job(rig: Rig, first_paid: Paid):
    before = rig.channel_state()
    r = rig.post(rig.wire(rig.new_body("someone else's job")), first_paid.ticket)
    assert r.status_code == 402
    assert r.json()["reason"] == "request_hash_mismatch"
    assert rig.channel_state() == before


def test_step4_a_used_nonce_cannot_be_reused_for_another_job(rig: Rig, first_paid: Paid):
    before = rig.channel_state()
    price = first_paid.price or 1
    paid = rig.send_voucher(
        rig.new_body("nonce reuse"), first_paid.voucher["nonce"], int(first_paid.voucher["cumulativeAmount"]) + price
    )
    assert paid.response.status_code == 402
    assert paid.response.json()["reason"] == "nonce_not_increasing"
    assert rig.channel_state() == before


# ===========================================================================
# Step 5: lower nonce
# ===========================================================================


def test_step5_a_voucher_with_a_lower_nonce_is_rejected(rig: Rig, first_paid: Paid):
    # Make sure the channel has at least two accepted nonces, so a strictly LOWER (but valid, >= 1) one exists.
    second = rig.pay(rig.new_body("second"))
    assert second.response.status_code == 200, second.response.text
    last_nonce = second.voucher["nonce"]
    assert last_nonce >= 2

    state = rig.channel_state()
    assert state["lastNonce"] == last_nonce
    price = second.price or 1

    stale = rig.send_voucher(
        rig.new_body("stale"),
        nonce=last_nonce - 1,
        cumulative=int(state["lastCumulativeAmount"]) + price,  # otherwise valid: only the nonce is wrong
    )
    assert stale.response.status_code == 402, stale.response.text
    body = stale.response.json()
    assert body["reason"] == "nonce_not_increasing"
    assert body["accepts"][0]["minNonce"] == last_nonce + 1  # tells the client what to send next
    assert rig.channel_state() == state  # nothing changed