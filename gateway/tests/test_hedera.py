"""Phase 3 tests: HCS proofs, SDK wrapper, durable outbox / publisher, and gateway integration.

The real Hedera network is NOT reachable from CI, so:
  * the SDK wrapper is tested with the REAL hiero-sdk-python classes, with only the network call
    (`Transaction.execute`) replaced - so argument names, types and flags are checked against the SDK;
  * the publisher and gateway are tested with an in-memory fake `AuditLogger`.
Live confirmation on testnet is the manual verify step (scripts/verify_hcs_message.py).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from eth_account import Account

from app.canonical import request_hash
from app.hedera_logger import (
    HCS_MAX_MESSAGE_BYTES,
    HcsError,
    HcsOutcome,
    HcsPublisher,
    HcsReceipt,
    HederaAuditLogger,
    HederaSettings,
    ProofError,
    build_proof,
    build_publisher_from_env,
    decode_mirror_message,
    encode_proof,
    load_operator_key,
    mirror_message_url,
    verify_proof,
)
from app.main import create_app
from conftest import AGENT_KEY, Harness, make_settings

AGENT_ADDRESS = Account.from_key(AGENT_KEY).address
HASH_A, HASH_B = "0x" + "aa" * 32, "0x" + "bb" * 32
CHANNEL = "0x" + "cc" * 32


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def sample_proof(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = dict(
        agent_id=1, channel_id=CHANNEL, nonce=7, request_hash=HASH_A, response_hash=HASH_B, ts=1_900_000_000
    )
    fields.update(overrides)
    return build_proof(AGENT_KEY, **fields)


class FakeLogger:
    """In-memory AuditLogger.  Scriptable: fail_next failures, a per-call delay, hard-down mode."""

    topic_id = "0.0.4242"

    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.fail_next = 0
        self.always_fail = False
        self.delay = 0.0
        self.calls = 0
        self._seq = 100

    async def submit(self, message: bytes) -> HcsReceipt:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.always_fail:
            raise HcsError("simulated outage")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise HcsError("simulated blip")
        self._seq += 1
        self.messages.append(message)
        return HcsReceipt(self.topic_id, self._seq)

    async def aclose(self) -> None:
        return None


def fast_publisher(logger: Any, db_path: str, **overrides: Any) -> HcsPublisher:
    options: dict[str, Any] = dict(
        wait_seconds=3.0, max_attempts=4, retry_base_seconds=0.03, retry_max_seconds=0.1, poll_seconds=0.03
    )
    options.update(overrides)
    return HcsPublisher(logger, db_path, **options)


# ===========================================================================
# A. proofs
# ===========================================================================


def test_proof_roundtrip_and_signer():
    proof = sample_proof()
    assert set(proof) == {"agentId", "channelId", "nonce", "requestHash", "responseHash", "ts", "gatewaySig"}
    assert verify_proof(proof) == AGENT_ADDRESS
    assert verify_proof(proof, expected_signer=AGENT_ADDRESS) == AGENT_ADDRESS
    assert verify_proof(proof, expected_signer=AGENT_ADDRESS.lower()) == AGENT_ADDRESS


def test_proof_rejects_the_wrong_signer():
    with pytest.raises(ProofError, match="expected"):
        verify_proof(sample_proof(), expected_signer=Account.create().address)


@pytest.mark.parametrize(
    "field, value",
    [("agentId", 2), ("channelId", "0x" + "dd" * 32), ("nonce", 8), ("requestHash", HASH_B), ("responseHash", HASH_A), ("ts", 1)],
)
def test_tampering_with_any_signed_field_changes_the_signer(field, value):
    proof = sample_proof()
    proof[field] = value
    try:
        recovered = verify_proof(proof)
    except ProofError:
        return  # also acceptable: signature no longer valid at all
    assert recovered != AGENT_ADDRESS


def test_proof_must_have_exactly_the_known_fields():
    proof = sample_proof()
    with pytest.raises(ProofError):
        verify_proof({**proof, "output": "leaked"})
    with pytest.raises(ProofError):
        verify_proof({k: v for k, v in proof.items() if k != "ts"})


def test_garbage_signature_is_a_proof_error_not_a_crash():
    proof = sample_proof()
    for bad in ("0x", "0xzz", "0x" + "00" * 65, 123):
        with pytest.raises(ProofError):
            verify_proof({**proof, "gatewaySig": bad})


def test_encoded_proof_is_canonical_and_comfortably_inside_the_hcs_limit():
    text = encode_proof(sample_proof())
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    assert len(text.encode()) < 600
    # Worst case: maximum-width numbers everywhere.
    worst = encode_proof(sample_proof(agent_id=2**53 - 1, nonce=2**53 - 1, ts=2**53 - 1))
    assert len(worst.encode()) <= HCS_MAX_MESSAGE_BYTES


def test_oversized_proofs_are_refused_before_they_reach_hedera():
    with pytest.raises(ProofError, match="limit"):
        encode_proof({**sample_proof(), "padding": "x" * 2000})


def test_proofs_carry_no_task_content():
    forbidden = {"input", "output", "task", "params", "job_id"}
    assert not forbidden & set(sample_proof())


def test_mirror_helpers():
    assert mirror_message_url("testnet", "0.0.4242", 9) == "https://testnet.mirrornode.hedera.com/api/v1/topics/0.0.4242/messages/9"
    assert decode_mirror_message({"message": "aGVsbG8="}) == b"hello"
    with pytest.raises(ProofError):
        decode_mirror_message({"message": "!!!"})
    with pytest.raises(ProofError):
        decode_mirror_message({})


# ===========================================================================
# B. settings and operator keys
# ===========================================================================

RAW = "11" * 32
ECDSA_DER = "3030020100300706052b8104000a04220420" + RAW
ED25519_DER = "302e020100300506032b657004220420" + RAW


def pubkey(key: Any) -> str:
    return key.public_key().to_string_raw()


def test_der_keys_are_self_describing():
    ecdsa = load_operator_key(ECDSA_DER, None)
    ed = load_operator_key(ED25519_DER, None)
    assert pubkey(ecdsa) != pubkey(ed)
    assert pubkey(ecdsa) == pubkey(load_operator_key(RAW, "ecdsa"))
    assert pubkey(ed) == pubkey(load_operator_key("0x" + RAW, "ed25519"))


def test_raw_key_types_really_do_differ():
    """Documents the SDK footgun this module guards against."""
    assert pubkey(load_operator_key(RAW, "ecdsa")) != pubkey(load_operator_key(RAW, "ed25519"))


def test_raw_key_without_a_type_is_refused_at_settings_time():
    with pytest.raises(ValueError, match="OPERATOR_KEY_TYPE"):
        HederaSettings(_env_file=None, operator_id="0.0.1", operator_key=RAW)
    HederaSettings(_env_file=None, operator_id="0.0.1", operator_key=RAW, operator_key_type="ecdsa")  # ok
    HederaSettings(_env_file=None, operator_id="0.0.1", operator_key=ECDSA_DER)  # DER needs no type


def test_bad_key_material_errors_without_echoing_the_key():
    with pytest.raises(ValueError) as exc:
        load_operator_key("not-hex-" + "z" * 56, "ecdsa")
    assert "not-hex" not in str(exc.value)


def test_mainnet_is_not_an_accepted_network():
    with pytest.raises(ValueError):
        HederaSettings(_env_file=None, hedera_network="mainnet")
    assert HederaSettings(_env_file=None, hedera_network="previewnet").hedera_network == "previewnet"


@pytest.mark.parametrize("field", ["operator_id", "topic_id"])
def test_entity_ids_are_validated(field):
    with pytest.raises(ValueError):
        HederaSettings(_env_file=None, **{field: "1234"})
    assert HederaSettings(_env_file=None, **{field: "0.0.1234"}).model_dump()[field] == "0.0.1234"


def test_publisher_from_env_disabled_half_configured_and_enabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in ("OPERATOR_ID", "OPERATOR_KEY", "OPERATOR_KEY_TYPE", "TOPIC_ID"):
        monkeypatch.delenv(var, raising=False)
    db = str(tmp_path / "x.sqlite3")

    assert build_publisher_from_env(db) is None  # nothing set -> disabled

    monkeypatch.setenv("OPERATOR_ID", "0.0.1001")
    monkeypatch.setenv("OPERATOR_KEY", ECDSA_DER)
    with pytest.raises(RuntimeError, match="half-configured"):
        build_publisher_from_env(db)  # operator but no topic: almost certainly a forgotten create_topic step

    monkeypatch.setenv("TOPIC_ID", "0.0.4242")
    publisher = build_publisher_from_env(db)
    assert publisher is not None and publisher.topic_id == "0.0.4242"


def test_explicit_settings_never_pick_up_hedera_credentials_from_the_environment(monkeypatch, tmp_path):
    """A developer's real OPERATOR_* variables must not make unit tests talk to Hedera."""
    monkeypatch.setenv("OPERATOR_ID", "0.0.1001")
    monkeypatch.setenv("OPERATOR_KEY", ECDSA_DER)
    monkeypatch.setenv("TOPIC_ID", "0.0.4242")
    app = create_app(make_settings(str(tmp_path / "x.sqlite3")), chain=Harness(str(tmp_path / "y.sqlite3")).chain)
    assert app.state.hcs is None


# ===========================================================================
# C. SDK wrapper, using the REAL SDK classes with only execute() faked
# ===========================================================================


def sdk_logger(**settings: Any) -> HederaAuditLogger:
    hs = HederaSettings(_env_file=None, operator_id="0.0.1001", operator_key=ECDSA_DER, topic_id="0.0.4242", **settings)
    logger = HederaAuditLogger(hs)
    logger._make_client = lambda: object()  # type: ignore[method-assign]  # real client construction does network I/O
    return logger


def patch_execute(monkeypatch, result: Any = None, error: Exception | None = None) -> list[dict[str, Any]]:
    from hiero_sdk_python import TopicMessageSubmitTransaction

    seen: list[dict[str, Any]] = []

    def fake_execute(self, client, timeout=None, wait_for_receipt=True, validate_status=False):
        seen.append(
            dict(topic=str(self.topic_id), message=self.message, max_chunks=self.max_chunks, timeout=timeout,
                 wait_for_receipt=wait_for_receipt, validate_status=validate_status)
        )
        if error:
            raise error
        return result

    monkeypatch.setattr(TopicMessageSubmitTransaction, "execute", fake_execute)
    return seen


def ok_receipt(seq: int | None = 7) -> SimpleNamespace:
    from hiero_sdk_python import ResponseCode

    return SimpleNamespace(status=ResponseCode.SUCCESS, topic_sequence_number=seq)


def test_sdk_submit_passes_the_right_arguments(monkeypatch):
    seen = patch_execute(monkeypatch, ok_receipt(7))
    receipt = asyncio.run(sdk_logger().submit(b'{"hello":"mesh"}'))
    assert receipt == HcsReceipt("0.0.4242", 7)
    (call,) = seen
    assert call["topic"] == "0.0.4242"
    assert call["message"] == b'{"hello":"mesh"}'
    assert call["max_chunks"] == 1  # never silently chunk a proof
    assert call["validate_status"] is True  # SDK default is False, which would hide failed statuses
    assert call["wait_for_receipt"] is True
    assert call["timeout"] == 30.0


def test_sdk_submit_rejects_a_non_success_receipt_even_if_the_sdk_did_not_raise(monkeypatch):
    from hiero_sdk_python import ResponseCode

    failing = SimpleNamespace(status=ResponseCode.INVALID_SIGNATURE, topic_sequence_number=0)
    patch_execute(monkeypatch, failing)
    with pytest.raises(HcsError, match="INVALID_SIGNATURE"):
        asyncio.run(sdk_logger().submit(b"x"))


@pytest.mark.parametrize("seq", [None, 0, -1, "7"])
def test_sdk_submit_requires_a_real_sequence_number(monkeypatch, seq):
    patch_execute(monkeypatch, ok_receipt(seq))
    with pytest.raises(HcsError, match="sequence"):
        asyncio.run(sdk_logger().submit(b"x"))


def test_sdk_exceptions_become_hcs_errors(monkeypatch):
    patch_execute(monkeypatch, error=ConnectionError("node unreachable"))
    with pytest.raises(HcsError, match="ConnectionError: node unreachable"):
        asyncio.run(sdk_logger().submit(b"x"))


def test_oversized_message_never_reaches_the_sdk(monkeypatch):
    seen = patch_execute(monkeypatch, ok_receipt())
    with pytest.raises(HcsError, match="limit"):
        asyncio.run(sdk_logger().submit(b"x" * (HCS_MAX_MESSAGE_BYTES + 1)))
    assert seen == []


def test_client_is_built_lazily_once_and_reused(monkeypatch):
    patch_execute(monkeypatch, ok_receipt())
    built: list[int] = []
    logger = sdk_logger()
    logger._make_client = lambda: built.append(1) or object()  # type: ignore[method-assign]
    assert built == []  # constructing the logger did no I/O
    asyncio.run(logger.submit(b"a"))
    asyncio.run(logger.submit(b"b"))
    assert built == [1]


def test_sdk_logger_requires_complete_configuration():
    with pytest.raises(ValueError):
        HederaAuditLogger(HederaSettings(_env_file=None, operator_id="0.0.1001", operator_key=ECDSA_DER))  # no topic


def test_create_topic_sets_admin_and_submit_keys(monkeypatch):
    import importlib.util
    from pathlib import Path

    from hiero_sdk_python import PrivateKey, TopicCreateTransaction, TopicId

    spec = importlib.util.spec_from_file_location("create_topic", Path(__file__).resolve().parents[1] / "scripts" / "create_topic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    key = PrivateKey.generate_ecdsa()
    closed: list[bool] = []
    fake_client = SimpleNamespace(operator_private_key=key, close=lambda: closed.append(True))
    monkeypatch.setattr(module, "make_sdk_client", lambda hs: fake_client)

    captured: list[Any] = []

    def fake_execute(self, client, timeout=None, wait_for_receipt=True, validate_status=False):
        captured.append((self.admin_key, self.submit_key, self.topic_memo, validate_status))  # topic_memo, not .memo (tx memo)
        return SimpleNamespace(topic_id=TopicId.from_string("0.0.5555"))

    monkeypatch.setattr(TopicCreateTransaction, "execute", fake_execute)
    hs = HederaSettings(_env_file=None, operator_id="0.0.1001", operator_key=ECDSA_DER)

    assert module.create_topic(hs, memo="m") == "0.0.5555"
    admin, submit, memo, validate = captured[-1]
    expected = key.public_key().to_string_raw()
    assert admin.to_string_raw() == expected and submit.to_string_raw() == expected  # only the gateway can write
    assert memo == "m" and validate is True and closed == [True]

    assert module.create_topic(hs, open_submit=True) == "0.0.5555"
    assert captured[-1][1] is None  # explicit opt-in only


# ===========================================================================
# D. publisher / outbox
# ===========================================================================


def run(coro_factory: Callable[[], Any]) -> Any:
    return asyncio.run(coro_factory())


def test_publish_confirms_and_records_the_sequence(tmp_path):
    async def go():
        logger = FakeLogger()
        publisher = fast_publisher(logger, str(tmp_path / "o.sqlite3"))
        await publisher.start()
        try:
            outcome = await publisher.publish(CHANNEL, 1, '{"p":1}')
            assert outcome == HcsOutcome("confirmed", "0.0.4242", 101)
            assert logger.messages == [b'{"p":1}']
            assert await publisher.stats() == {"pending": 0, "confirmed": 1, "failed": 0}
        finally:
            await publisher.stop()

    run(go)


def test_failure_returns_pending_immediately_then_retries_until_confirmed_without_duplicates(tmp_path):
    async def go():
        logger = FakeLogger()
        logger.fail_next = 2
        seen: list[tuple[str, int, HcsOutcome]] = []

        async def callback(channel_id, nonce, outcome):
            seen.append((channel_id, nonce, outcome))

        publisher = fast_publisher(logger, str(tmp_path / "o.sqlite3"), on_outcome=callback)
        await publisher.start()
        try:
            started = time.monotonic()
            first = await publisher.publish(CHANNEL, 1, "{}")
            assert first.status == "pending"
            assert time.monotonic() - started < 1.0  # released on first failure, did not burn the whole wait
            for _ in range(200):
                if (await publisher.stats())["confirmed"]:
                    break
                await asyncio.sleep(0.02)
            assert (await publisher.stats())["confirmed"] == 1
            assert logger.calls == 3 and len(logger.messages) == 1  # 2 failures + 1 success, one message
            assert [o.status for _, _, o in seen] == ["confirmed"]
        finally:
            await publisher.stop()

    run(go)


def test_a_slow_attempt_returns_pending_but_is_not_resubmitted(tmp_path):
    """The request timing out must never trigger a second submission of the same proof."""

    async def go():
        logger = FakeLogger()
        logger.delay = 0.4
        publisher = fast_publisher(logger, str(tmp_path / "o.sqlite3"), wait_seconds=0.1)
        await publisher.start()
        try:
            assert (await publisher.publish(CHANNEL, 1, "{}")).status == "pending"
            for _ in range(200):
                if (await publisher.stats())["confirmed"]:
                    break
                await asyncio.sleep(0.02)
            assert (await publisher.stats())["confirmed"] == 1
            assert logger.calls == 1 and len(logger.messages) == 1
        finally:
            await publisher.stop()

    run(go)


def test_publishing_the_same_job_twice_submits_once(tmp_path):
    async def go():
        logger = FakeLogger()
        publisher = fast_publisher(logger, str(tmp_path / "o.sqlite3"))
        await publisher.start()
        try:
            a = await publisher.publish(CHANNEL, 1, '{"p":1}')
            b = await publisher.publish(CHANNEL, 1, '{"p":1}')
            assert a == b and a.status == "confirmed"
            assert len(logger.messages) == 1
            other_nonce = await publisher.publish(CHANNEL, 2, '{"p":2}')
            assert other_nonce.sequence_number == a.sequence_number + 1
        finally:
            await publisher.stop()

    run(go)


def test_gives_up_after_max_attempts_and_reports_failed(tmp_path):
    async def go():
        logger = FakeLogger()
        logger.always_fail = True
        seen: list[HcsOutcome] = []

        async def callback(_c, _n, outcome):
            seen.append(outcome)

        publisher = fast_publisher(logger, str(tmp_path / "o.sqlite3"), max_attempts=3, on_outcome=callback)
        await publisher.start()
        try:
            assert (await publisher.publish(CHANNEL, 1, "{}")).status == "pending"
            for _ in range(300):
                if (await publisher.stats())["failed"]:
                    break
                await asyncio.sleep(0.02)
            assert await publisher.stats() == {"pending": 0, "confirmed": 0, "failed": 1}
            assert logger.calls == 3
            assert [o.status for o in seen] == ["failed"]
            assert (await publisher.publish(CHANNEL, 1, "{}")).status == "failed"  # sticky, not silently retried
            assert logger.calls == 3
        finally:
            await publisher.stop()

    run(go)


def test_pending_proofs_survive_a_restart(tmp_path):
    db = str(tmp_path / "o.sqlite3")

    async def make_retry_due_now():
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE hcs_outbox SET next_attempt_at = 0")  # skip the long backoff the first run scheduled

    async def go():
        down = FakeLogger()
        down.always_fail = True
        first = fast_publisher(down, db, max_attempts=50, retry_base_seconds=5.0, retry_max_seconds=5.0)
        await first.start()
        assert (await first.publish(CHANNEL, 9, '{"p":9}')).status == "pending"
        await first.stop()  # "crash"
        await make_retry_due_now()

        healthy = FakeLogger()
        second = fast_publisher(healthy, db)
        await second.start()  # same database: resumes the pending proof on its own
        try:
            for _ in range(300):
                if (await second.stats())["confirmed"]:
                    break
                await asyncio.sleep(0.02)
            assert healthy.messages == [b'{"p":9}']
        finally:
            await second.stop()

    run(go)


def test_unexpected_exceptions_in_the_logger_do_not_kill_the_worker(tmp_path):
    class Exploding(FakeLogger):
        async def submit(self, message):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("not an HcsError")
            return await super().submit(message)

    async def go():
        logger = Exploding()
        publisher = fast_publisher(logger, str(tmp_path / "o.sqlite3"))
        await publisher.start()
        try:
            await publisher.publish(CHANNEL, 1, "{}")
            for _ in range(200):
                if (await publisher.stats())["confirmed"]:
                    break
                await asyncio.sleep(0.02)
            assert (await publisher.stats())["confirmed"] == 1
        finally:
            await publisher.stop()

    run(go)


def test_backoff_grows_and_is_capped(tmp_path):
    publisher = HcsPublisher(FakeLogger(), str(tmp_path / "o.sqlite3"), retry_base_seconds=2.0, retry_max_seconds=60.0)
    delays = [publisher._backoff(n) for n in range(1, 12)]
    assert 1.6 <= delays[0] <= 2.4
    assert 3.2 <= delays[1] <= 4.8
    assert all(d <= 60.0 * 1.2 for d in delays)
    assert delays[-1] >= 60.0 * 0.8  # reached the cap


# ===========================================================================
# E. gateway integration
# ===========================================================================


@pytest.fixture
def make_hcs_harness(tmp_path):
    created: list[Harness] = []

    def factory(logger: FakeLogger, name: str = "ledger.sqlite3", **publisher_options: Any) -> Harness:
        db = str(tmp_path / name)
        harness = Harness(db, hcs=fast_publisher(logger, db, **publisher_options))
        harness.client.__enter__()  # runs the app lifespan, which starts the HCS worker
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.client.__exit__(None, None, None)


def test_paid_job_returns_the_hcs_sequence_and_publishes_a_verifiable_compact_proof(make_hcs_harness):
    logger = FakeLogger()
    h = make_hcs_harness(logger)
    body = h.body(input="TOP-SECRET task content")
    r = h.paid_post(body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["hcs_status"], data["hcs_topic_id"], data["hcs_sequence_number"]) == ("confirmed", "0.0.4242", 101)

    (message,) = logger.messages
    assert len(message) <= HCS_MAX_MESSAGE_BYTES
    assert b"TOP-SECRET" not in message  # no task content on HCS
    proof = json.loads(message)
    assert set(proof) == {"agentId", "channelId", "nonce", "requestHash", "responseHash", "ts", "gatewaySig"}
    assert proof["requestHash"] == data["request_hash"] == request_hash(body)
    assert proof["responseHash"] == data["response_hash"]
    assert (proof["channelId"], proof["nonce"], proof["agentId"]) == (h.channel_id, 1, 1)
    assert abs(proof["ts"] - time.time()) < 30
    assert verify_proof(proof, expected_signer=h.settings.agent_signer_address)  # signed by the registered agent key


def test_hedera_outage_does_not_fail_or_lose_the_job_and_the_sequence_arrives_later(make_hcs_harness):
    logger = FakeLogger()
    logger.fail_next = 2
    h = make_hcs_harness(logger)
    body = h.body()
    voucher = h.voucher(body, nonce=1)
    ticket = h.header(voucher, h.sign(voucher))
    raw = json.dumps(body).encode()

    r = h.post_raw(raw, ticket)
    assert r.status_code == 200  # the paid job succeeded regardless
    data = r.json()
    assert data["output"] == "hello mesh"
    assert (data["hcs_status"], data["hcs_topic_id"], data["hcs_sequence_number"]) == ("pending", "0.0.4242", None)
    assert h.state()["lastNonce"] == 1  # charged exactly once

    wait_until(lambda: len(logger.messages) == 1)
    wait_until(lambda: h.post_raw(raw, ticket).json()["hcs_status"] == "confirmed")

    replay = h.post_raw(raw, ticket).json()  # idempotent retry now reports the settled HCS state
    assert replay["idempotent_replay"] is True
    assert replay["job_id"] == data["job_id"]
    assert replay["hcs_sequence_number"] == 101
    assert len(logger.messages) == 1  # one proof for one job
    assert h.state()["lastNonce"] == 1


def test_pending_proof_is_delivered_after_a_gateway_restart(make_hcs_harness, tmp_path):
    down = FakeLogger()
    down.always_fail = True
    h1 = make_hcs_harness(down, "shared.sqlite3", max_attempts=100, retry_base_seconds=60.0, retry_max_seconds=60.0)
    body = h1.body()
    voucher = h1.voucher(body, nonce=1)
    ticket = h1.header(voucher, h1.sign(voucher))
    raw = json.dumps(body).encode()
    assert h1.post_raw(raw, ticket).json()["hcs_status"] == "pending"
    h1.client.__exit__(None, None, None)  # gateway stops

    import sqlite3

    with sqlite3.connect(str(tmp_path / "shared.sqlite3")) as conn:
        conn.execute("UPDATE hcs_outbox SET next_attempt_at = 0")  # skip the 60s backoff for the test

    healthy = FakeLogger()
    h2 = make_hcs_harness(healthy, "shared.sqlite3")  # new process, same database
    h2.chain.channels = h1.chain.channels
    h2.payer, h2.channel_id = h1.payer, h1.channel_id
    wait_until(lambda: len(healthy.messages) == 1)
    wait_until(lambda: h2.post_raw(raw, ticket).json()["hcs_status"] == "confirmed")
    assert h2.post_raw(raw, ticket).json()["hcs_sequence_number"] == 101


def test_health_reports_hcs_backlog_and_failure(make_hcs_harness):
    logger = FakeLogger()
    logger.always_fail = True
    h = make_hcs_harness(logger, max_attempts=2)
    assert h.client.get("/health").json()["hcs"] == "ok"

    assert h.paid_post().json()["hcs_status"] == "pending"
    wait_until(lambda: h.client.get("/health").json()["hcs"] == "pending:0,failed:1")
    health = h.client.get("/health").json()
    assert health["status"] == "degraded"  # a proof that could not be delivered needs a human


def test_hcs_disabled_is_reported_and_costs_nothing(tmp_path):
    h = Harness(str(tmp_path / "l.sqlite3"))
    data = h.paid_post().json()
    assert (data["hcs_status"], data["hcs_topic_id"], data["hcs_sequence_number"]) == ("disabled", None, None)
    assert h.client.get("/health").json()["hcs"] == "disabled"
    h.client.close()


def test_a_failed_job_publishes_nothing(make_hcs_harness, monkeypatch):
    import app.main as main

    logger = FakeLogger()
    h = make_hcs_harness(logger)
    monkeypatch.setattr(main, "run_task", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    assert h.paid_post().status_code == 500
    time.sleep(0.2)
    assert logger.messages == []  # no proof for work that was not done / not charged