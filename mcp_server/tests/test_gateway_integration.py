"""Cross-package tests: this client against the REAL gateway app (served in-process over ASGI), no sockets.

Skipped automatically when the gateway package or its dependencies are not available.  What this proves that a fake
gateway cannot: header encoding, canonical hashing, the EIP-712 domain, low-s signatures, replay protection,
idempotency and the 402 challenge shape all agree between the two independently written implementations.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "gateway"
pytest.importorskip("fastapi")
pytest.importorskip("web3")
if not (GATEWAY_DIR / "app" / "main.py").exists():
    pytest.skip("gateway package not found next to mcp_server/", allow_module_level=True)
sys.path.insert(0, str(GATEWAY_DIR))

from app import canonical as gw_canonical  # noqa: E402
from app.config import Settings as GatewaySettings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.registry import STATUS_OPEN, AgentRecord, ChannelRecord  # noqa: E402

from conftest import CHANNEL_CONTRACT, CHANNEL_ID, PAYER, make_settings, run  # noqa: E402
from server import AmxClient, AmxToolError  # noqa: E402
from signer import canonical_bytes, request_hash, voucher_digest, voucher_typed_data  # noqa: E402

AGENT_KEY = "0x" + "11" * 32
OWNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
DEPOSIT = 10**6


class FakeChain:
    """Minimal ChainReader for the real gateway: one registered agent, one funded open channel owned by PAYER."""

    def __init__(self, gateway_settings: GatewaySettings, payer: str = PAYER.address) -> None:
        self.agent = AgentRecord(
            agent_id=1, owner=OWNER, agent_uri="https://agent.test/card.json",
            pub_key=bytes.fromhex(gateway_settings.agent_pubkey_hex[2:]), signer=gateway_settings.agent_signer_address,
        )
        self.channel = ChannelRecord(payer=payer, status=STATUS_OPEN, challenge_period=3600, close_deadline=0, agent_id=1, deposit=DEPOSIT, claimed=0)

    async def get_agent(self, agent_id):
        return self.agent

    async def get_channel(self, channel_id):
        return self.channel if channel_id == CHANNEL_ID else None

    async def aclose(self):
        return None


class Rig:
    def __init__(self, tmp_path, *, price=1000, payer=PAYER.address, wrap=None, hcs=None, **client_overrides):
        self.gateway_settings = GatewaySettings(
            _env_file=None, rpc_url="http://fake.invalid", chain_network="base-sepolia", chain_id=84532,
            identity_contract="0x5FbDB2315678afecb367f032d93F642f64180aa3", channel_contract=CHANNEL_CONTRACT,
            agent_id=1, agent_owner_address=OWNER, agent_private_key=AGENT_KEY, price_per_call=price,
            min_accepted_challenge_period=3600, db_path=str(tmp_path / "gateway.sqlite3"), rate_limit_per_minute=10_000,
        )
        self.app = create_app(self.gateway_settings, chain=FakeChain(self.gateway_settings, payer), hcs=hcs)
        transport: httpx.AsyncBaseTransport = httpx.ASGITransport(app=self.app)
        self.transport = wrap(transport) if wrap else transport
        self.settings = make_settings(tmp_path, **client_overrides)
        self.http = httpx.AsyncClient(transport=self.transport, base_url="http://gateway")
        self.client = AmxClient(self.settings, http=self.http)

    @property
    def ledger(self):
        return self.app.state.amx.ledger

    def execute(self, task="echo", text="hello mesh", params=None):
        return run(self.client.execute(task, text, params))


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ===========================================================================
# the two implementations agree
# ===========================================================================

CORPUS = [
    {}, {"a": 1}, {"b": 1, "a": [True, None, "é", "日本語", "\u2028"]}, {"task": "echo", "input": "x" * 500},
    {"nested": {"z": {"y": [1, 2, {"x": -7}]}}, "n": 9007199254740991}, {"quote": 'he said "hi" \\ \n \t'},
]


@pytest.mark.parametrize("obj", CORPUS)
def test_canonical_json_and_request_hash_are_byte_identical(obj):
    assert canonical_bytes(obj) == gw_canonical.canonical_bytes(obj)
    assert request_hash(obj) == gw_canonical.request_hash(obj)


@pytest.mark.parametrize("bad", [{"a": 1.5}, {"a": 2**60}, {1: "x"}, {"a": "\ud800"}])
def test_both_reject_the_same_inputs(bad):
    with pytest.raises(Exception):
        canonical_bytes(bad)
    with pytest.raises(gw_canonical.CanonicalizationError):
        gw_canonical.canonical_bytes(bad)


def test_eip712_digest_and_recovery_match_across_implementations():
    voucher = {
        "channelId": CHANNEL_ID, "agentId": 3, "cumulativeAmount": "123456789012345678901234567890",
        "nonce": 12, "expiry": 1_900_000_000, "requestHash": "0x" + "cd" * 32,
    }
    domain = gw_canonical.VoucherDomain(chain_id=84532, verifying_contract=CHANNEL_CONTRACT)
    assert voucher_digest(84532, CHANNEL_CONTRACT, voucher) == gw_canonical.voucher_digest(domain, voucher)
    assert voucher_typed_data(84532, CHANNEL_CONTRACT, voucher) == gw_canonical.voucher_typed_data(domain, voucher)


# ===========================================================================
# end to end against the real gateway
# ===========================================================================


def test_two_paid_calls_against_the_real_gateway(rig: Rig):
    first = rig.execute(text="first")
    second = rig.execute("sha256", "second")
    assert first.output == "first" and len(second.output) == 64  # sha256 hex
    assert [first.payment.nonce, second.payment.nonce] == [1, 2]
    assert second.payment.cumulative_amount == "2000"
    state = rig.ledger.get_state(CHANNEL_ID)
    assert (state.last_nonce, state.last_cumulative) == (2, 2000)  # the gateway's books match the signer's
    assert rig.client.signer.session_spent == 2000
    assert first.hcs.status == "disabled"  # no Hedera in this rig


def test_the_gateway_stores_a_voucher_the_payee_could_settle_on_chain(rig: Rig):
    rig.execute()
    stored = rig.ledger.latest_voucher(CHANNEL_ID)
    assert stored is not None
    voucher, signature = stored
    domain = gw_canonical.VoucherDomain(chain_id=84532, verifying_contract=CHANNEL_CONTRACT)
    assert gw_canonical.recover_voucher_signer(domain, voucher, signature) == PAYER.address  # low-s, recoverable


def test_a_lost_response_is_recovered_without_a_second_charge(tmp_path):
    """The gateway processes the request, the response is lost; repeating the task re-sends the SAME ticket."""

    class DropFirstPaidResponse(httpx.AsyncBaseTransport):
        def __init__(self, inner):
            self.inner, self.dropped = inner, False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if "x-402-payment-ticket" in request.headers and not self.dropped:
                self.dropped = True
                await response.aread()
                raise httpx.ReadTimeout("response lost in transit")
            return response

    rig = Rig(tmp_path, wrap=DropFirstPaidResponse)
    with pytest.raises(AmxToolError) as exc:
        rig.execute(text="important")
    assert exc.value.code == "outcome_unknown"
    assert rig.ledger.get_state(CHANNEL_ID).last_nonce == 1  # the gateway DID process it

    result = rig.execute(text="important")
    assert result.idempotent_replay is True and result.payment.ticket_reused is True
    state = rig.ledger.get_state(CHANNEL_ID)
    assert (state.last_nonce, state.last_cumulative) == (1, 1000)  # charged exactly once on the real ledger
    assert rig.client.signer.session_spent == 1000


def test_a_lying_challenge_cannot_make_the_real_client_overspend(tmp_path):
    """A man-in-the-middle inflates minCumulativeAmount; the real gateway never sees a ticket."""

    class InflateChallenge(httpx.AsyncBaseTransport):
        def __init__(self, inner):
            self.inner = inner

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if response.status_code == 402:
                body = json.loads(await response.aread())
                body["accepts"][0]["minCumulativeAmount"] = str(10**18)
                body["accepts"][0]["minNonce"] = 50
                return httpx.Response(402, json=body)
            return response

    rig = Rig(tmp_path, wrap=InflateChallenge)
    with pytest.raises(AmxToolError) as exc:
        rig.execute()
    assert exc.value.code == "state_ahead_of_signer"
    assert rig.ledger.get_state(CHANNEL_ID).last_nonce == 0
    assert rig.client.signer.session_spent == 0


def test_a_gateway_price_above_the_cap_is_refused_before_signing(tmp_path):
    rig = Rig(tmp_path, price=50_000, max_price_per_call=10_000)
    with pytest.raises(AmxToolError) as exc:
        rig.execute()
    assert exc.value.code == "price_exceeds_cap"
    assert rig.ledger.get_state(CHANNEL_ID).last_nonce == 0


def test_a_ticket_from_the_wrong_payer_is_rejected_by_the_real_gateway_and_rolled_back(tmp_path):
    other = "0x" + "99" * 20  # the channel on-chain belongs to someone else
    rig = Rig(tmp_path, payer=other)
    with pytest.raises(AmxToolError) as exc:
        rig.execute()
    assert exc.value.code == "signer_not_payer" and "not charged" in exc.value.message
    assert rig.client.signer.session_spent == 0
    assert rig.ledger.get_state(CHANNEL_ID).last_nonce == 0


def test_the_signer_resumes_correctly_after_a_gateway_side_rollback(tmp_path):
    """The gateway releases a voucher when a job fails; our next voucher must still be accepted."""
    import app.main as gateway_main

    rig = Rig(tmp_path)
    rig.execute(text="ok")
    original = gateway_main.run_task
    gateway_main.run_task = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        with pytest.raises(AmxToolError) as exc:
            rig.execute(text="fails")
    finally:
        gateway_main.run_task = original
    assert exc.value.code == "execution_failed"
    assert rig.client.signer.session_spent == 1000  # the failed job was not charged to us
    assert rig.ledger.get_state(CHANNEL_ID).last_nonce == 1

    nxt = rig.execute(text="fine again")
    assert (nxt.payment.nonce, nxt.payment.cumulative_amount) == (2, "2000")


# ===========================================================================
# HCS fields as produced by the real gateway (Hedera itself replaced by an in-memory logger)
# ===========================================================================


class StubHedera:
    """AuditLogger that 'reaches consensus' instantly, or after a delay."""

    topic_id = "0.0.4242"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay, self.messages, self._seq = delay, [], 700

    async def submit(self, message: bytes):
        import asyncio

        from app.hedera_logger import HcsReceipt

        if self.delay:
            await asyncio.sleep(self.delay)
        self._seq += 1
        self.messages.append(message)
        return HcsReceipt(self.topic_id, self._seq)

    async def aclose(self):
        return None


def test_the_real_gateways_confirmed_hcs_proof_reaches_the_tool_result(tmp_path):
    import asyncio

    from app.hedera_logger import HcsPublisher, verify_proof

    async def scenario():
        hedera = StubHedera()
        publisher = HcsPublisher(hedera, str(tmp_path / "gateway.sqlite3"), wait_seconds=3.0, poll_seconds=0.02)
        rig = Rig(tmp_path, hcs=publisher)
        await publisher.start()
        try:
            return hedera, await rig.client.execute("echo", "audited", None), rig
        finally:
            await publisher.stop()

    hedera, result, rig = asyncio.run(scenario())
    assert (result.hcs.status, result.hcs.topic_id, result.hcs.sequence_number) == ("confirmed", "0.0.4242", 701)
    assert result.hcs.hashscan_url == "https://hashscan.io/testnet/topic/0.0.4242"
    proof = json.loads(hedera.messages[0])  # and the proof on the topic is the one this call produced
    assert proof["requestHash"] == result.request_hash and proof["responseHash"] == result.response_hash
    assert (proof["channelId"], proof["nonce"]) == (CHANNEL_ID, result.payment.nonce)
    assert verify_proof(proof, expected_signer=rig.gateway_settings.agent_signer_address)


def test_a_slow_hedera_surfaces_as_pending_not_as_a_failure(tmp_path):
    import asyncio

    from app.hedera_logger import HcsPublisher

    async def scenario():
        hedera = StubHedera(delay=1.0)
        publisher = HcsPublisher(hedera, str(tmp_path / "gateway.sqlite3"), wait_seconds=0.1, poll_seconds=0.02)
        rig = Rig(tmp_path, hcs=publisher)
        await publisher.start()
        try:
            return await rig.client.execute("echo", "slow audit", None)
        finally:
            await publisher.stop()

    result = asyncio.run(scenario())
    assert result.output == "slow audit"  # the paid job is delivered regardless
    assert (result.hcs.status, result.hcs.topic_id, result.hcs.sequence_number) == ("pending", "0.0.4242", None)
    assert result.payment.cumulative_amount == "1000"  # and charged exactly once