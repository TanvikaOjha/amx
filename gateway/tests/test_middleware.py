"""Phase 2 tests: one test per rejection case, plus happy path, idempotency and persistence."""

from __future__ import annotations

import json
import time

import pytest
from eth_account import Account

from app.main import create_app
from app.schemas import PaymentChallenge, TaskExecutionResponse
from conftest import CHANNEL, DEPOSIT, PRICE, Harness

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def assert_402(response, reason: str | None) -> dict:
    assert response.status_code == 402, response.text
    body = response.json()
    PaymentChallenge.model_validate(body)  # the challenge is always schema-valid
    assert body["error"] == "payment_required"
    assert body.get("reason") == reason
    assert response.headers["www-authenticate"].startswith("amx-voucher")
    return body


# ---------------------------------------------------------------------------
# 402 challenge
# ---------------------------------------------------------------------------


def test_no_ticket_returns_challenge(h: Harness):
    r = h.client.post("/v1/execute", json=h.body())
    body = assert_402(r, None)
    assert body["accepts"] == [
        {
            "scheme": "amx-voucher",
            "network": "base-sepolia",
            "channelContract": h.settings.channel_contract,
            "agentId": 1,
            "pricePerCall": "1000",
            "asset": "native",
            "minNonce": 1,
            "minCumulativeAmount": "1000",
        }
    ]


def test_no_ticket_is_402_even_without_a_valid_body(h: Harness):
    """Clients must always be able to discover the price."""
    assert_402(h.client.post("/v1/execute", content=b"not json"), None)
    assert_402(h.client.post("/v1/execute"), None)


def test_challenge_is_channel_specific_when_hinted(h: Harness):
    assert h.paid_post(nonce=1, cumulative=1000).status_code == 200
    r = h.client.post("/v1/execute", json=h.body(), headers={"X-AMX-Channel-Id": h.channel_id})
    accept = assert_402(r, None)["accepts"][0]
    assert (accept["minNonce"], accept["minCumulativeAmount"]) == (2, "2000")


@pytest.mark.parametrize("ticket", ["!!!not-base64!!!", "bm90IGpzb24", "e30", "W10"])
def test_malformed_ticket(h: Harness, ticket: str):
    assert_402(h.post_raw(json.dumps(h.body()).encode(), ticket), "malformed_ticket")


def test_ticket_with_extra_field_is_malformed(h: Harness):
    body = h.body()
    voucher = h.voucher(body)
    voucher["surprise"] = 1
    assert_402(h.post_raw(json.dumps(body).encode(), h.header(voucher, h.sign(h.voucher(body)))), "malformed_ticket")


def test_ticket_with_uppercase_hex_is_malformed(h: Harness):
    body = h.body()
    voucher = h.voucher(body)
    voucher["channelId"] = voucher["channelId"].upper().replace("0X", "0x")
    assert_402(h.post_raw(json.dumps(body).encode(), h.header(voucher, h.sign(h.voucher(body)))), "malformed_ticket")


def test_oversized_ticket_is_malformed(h: Harness):
    assert_402(h.post_raw(json.dumps(h.body()).encode(), "A" * 5000), "malformed_ticket")


def test_wrong_agent(h: Harness):
    assert_402(h.paid_post(agentId=2), "wrong_agent")


def test_request_hash_mismatch(h: Harness):
    """A ticket bound to one body cannot pay for another."""
    other = h.body(input="a different job")
    voucher = h.voucher(other)
    ticket = h.header(voucher, h.sign(voucher))
    assert_402(h.post_raw(json.dumps(h.body()).encode(), ticket), "request_hash_mismatch")


def test_expired_voucher(h: Harness):
    assert_402(h.paid_post(expiry=int(time.time()) - 1), "expired")


def test_garbage_signature(h: Harness):
    body = h.body()
    voucher = h.voucher(body)
    assert_402(h.post_raw(json.dumps(body).encode(), h.header(voucher, "0x" + "00" * 65)), "invalid_signature")


def test_high_s_signature_is_rejected_because_the_contract_would_reject_it(h: Harness):
    body = h.body()
    voucher = h.voucher(body)
    ticket = h.header(voucher, h.high_s(h.sign(voucher)))
    assert_402(h.post_raw(json.dumps(body).encode(), ticket), "invalid_signature")


def test_signature_from_someone_other_than_the_payer(h: Harness):
    body = h.body()
    voucher = h.voucher(body)
    ticket = h.header(voucher, h.sign(voucher, account=Account.create()))
    assert_402(h.post_raw(json.dumps(body).encode(), ticket), "signer_not_payer")


def test_voucher_tampered_after_signing(h: Harness):
    body = h.body()
    signed = h.voucher(body, cumulative=PRICE)
    signature = h.sign(signed)
    tampered = {**signed, "cumulativeAmount": "1"}
    assert_402(h.post_raw(json.dumps(body).encode(), h.header(tampered, signature)), "signer_not_payer")


def test_unknown_channel(h: Harness):
    h.chain.channels.clear()
    assert_402(h.paid_post(), "unknown_channel")


@pytest.mark.parametrize("status", [0 + 2, 3])  # Closing, Closed
def test_channel_not_open(h: Harness, status: int):
    h.set_channel(status=status)
    assert_402(h.paid_post(), "channel_not_open")


def test_channel_for_another_agent(h: Harness):
    h.set_channel(agent_id=2)
    assert_402(h.paid_post(), "channel_agent_mismatch")


def test_challenge_period_too_short(h: Harness):
    h.set_channel(challenge_period=60)
    assert_402(h.paid_post(), "challenge_period_too_short")


def test_amount_exceeds_deposit(h: Harness):
    assert_402(h.paid_post(cumulative=DEPOSIT + 1), "exceeds_deposit")
    assert h.paid_post(cumulative=DEPOSIT).status_code == 200  # exactly the deposit is fine


# ---------------------------------------------------------------------------
# replay / monotonicity
# ---------------------------------------------------------------------------


def test_happy_path_and_canonical_hash_ignores_wire_formatting(h: Harness):
    r = h.paid_post(h.body(params={"z": 1, "a": "x"}))  # sent with indent=2 and unsorted keys
    assert r.status_code == 200, r.text
    result = TaskExecutionResponse.model_validate(r.json())
    assert result.output == "hello mesh"
    assert (result.nonce, result.cumulative_amount, result.charged_amount) == (1, "1000", "1000")
    assert result.idempotent_replay is False
    assert result.hcs_status == "disabled"


def test_lower_or_equal_nonce_is_rejected(h: Harness):
    assert h.paid_post(h.body(input="one"), nonce=5, cumulative=1000).status_code == 200
    for nonce in (5, 4, 1):
        r = h.paid_post(h.body(input=f"job {nonce}"), nonce=nonce, cumulative=9000)
        body = assert_402(r, "nonce_not_increasing")
        assert body["accepts"][0]["minNonce"] == 6  # tells the client what to send next


def test_cumulative_must_grow_by_at_least_the_price(h: Harness):
    assert h.paid_post(h.body(input="one"), nonce=1, cumulative=1000).status_code == 200
    body = assert_402(h.paid_post(h.body(input="two"), nonce=2, cumulative=1999), "insufficient_cumulative_amount")
    assert body["accepts"][0]["minCumulativeAmount"] == "2000"
    assert h.paid_post(h.body(input="two"), nonce=2, cumulative=2000).status_code == 200


def test_a_cumulative_jump_is_accepted(h: Harness):
    """Paying more than the minimum (e.g. prepaying) is allowed; paying less is not."""
    assert h.paid_post(nonce=1, cumulative=5000).status_code == 200
    assert h.state()["minCumulativeAmount"] == "6000"


def test_same_nonce_with_different_body_is_rejected(h: Harness):
    assert h.paid_post(h.body(input="first"), nonce=1).status_code == 200
    assert_402(h.paid_post(h.body(input="second"), nonce=1), "nonce_not_increasing")


def test_idempotent_retry_returns_original_without_second_charge(h: Harness):
    body = h.body()
    voucher = h.voucher(body, nonce=1, cumulative=PRICE)
    ticket = h.header(voucher, h.sign(voucher))
    raw = json.dumps(body).encode()

    first = h.post_raw(raw, ticket)
    assert first.status_code == 200
    state_after_first = h.state()

    second = h.post_raw(raw, ticket)
    assert second.status_code == 200
    assert second.headers["x-amx-idempotent-replay"] == "true"
    assert second.json()["idempotent_replay"] is True
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["response_hash"] == first.json()["response_hash"]
    assert h.state() == state_after_first  # nothing more was charged


def test_idempotent_retry_still_works_after_the_voucher_expires_or_channel_closes(h: Harness):
    body = h.body()
    voucher = h.voucher(body, nonce=1, cumulative=PRICE, expiry=int(time.time()) + 1)
    ticket = h.header(voucher, h.sign(voucher))
    raw = json.dumps(body).encode()
    first = h.post_raw(raw, ticket)
    assert first.status_code == 200

    h.set_channel(status=2)  # payer started a unilateral close
    time.sleep(1.2)  # voucher now expired
    again = h.post_raw(raw, ticket)
    assert again.status_code == 200
    assert again.json()["job_id"] == first.json()["job_id"]


def test_in_flight_duplicate_gets_409(h: Harness):
    body = h.body()
    voucher = h.voucher(body, nonce=1, cumulative=PRICE)
    signature = h.sign(voucher)
    ledger = h.app.state.amx.ledger
    from app.canonical import request_hash

    ledger.reserve(
        channel_id=h.channel_id, nonce=1, cumulative=PRICE, request_hash=request_hash(body),
        signer=h.payer.address, voucher_json="{}", signature=signature, price=PRICE,
    )  # simulates a request that is still executing
    r = h.post_raw(json.dumps(body).encode(), h.header(voucher, signature))
    assert r.status_code == 409
    assert r.json()["error"] == "request_in_progress"


def test_nonces_survive_a_restart(h: Harness, tmp_path):
    assert h.paid_post(nonce=7, cumulative=7000).status_code == 200

    restarted = create_app(h.settings, chain=h.chain)  # new process, same SQLite file
    from fastapi.testclient import TestClient

    client = TestClient(restarted)
    body = h.body(input="after restart")
    voucher = h.voucher(body, nonce=7, cumulative=8000)
    r = client.post(
        "/v1/execute", content=json.dumps(body).encode(), headers={"X-402-Payment-Ticket": h.header(voucher, h.sign(voucher))}
    )
    assert_402(r, "nonce_not_increasing")
    assert client.get(f"/v1/channels/{h.channel_id}").json()["lastNonce"] == 7


def test_channels_are_isolated_from_each_other(h: Harness):
    assert h.paid_post(nonce=1).status_code == 200
    other_id = "0x" + "ee" * 32
    h.chain.channels[other_id] = h.open_channel_record()
    body = h.body()
    voucher = h.voucher(body, nonce=1, channelId=other_id)  # nonce 1 on a different channel is fine
    r = h.post_raw(json.dumps(body).encode(), h.header(voucher, h.sign(voucher)))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# body validation (422, never charged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b'{"task": "echo", "input": "x", "extra": 1}',  # extra="forbid"
        b'{"task": "nope", "input": "x"}',  # unknown task
        b'{"task": "echo"}',  # missing input
        b'{"task": "echo", "input": 5}',  # strict types: no int -> str coercion
        b'{"task": "echo", "input": "x", "params": {"a": 1.5}}',  # floats banned
        b'{"task": "echo", "input": "x", "params": {"bad key": 1}}',  # key pattern
        b'{"task": "echo", "input": "x", "input": "y"}',  # duplicate keys
        b'{"task": "echo", "input": "x", "params": {"a": null}}',  # null value
        b"[]",
        b"not json",
    ],
)
def test_invalid_bodies_are_422_and_do_not_spend_the_voucher(h: Harness, raw: bytes):
    valid_body = h.body()
    voucher = h.voucher(valid_body, nonce=1)  # the hash will not match; 422 must come first
    r = h.post_raw(raw, h.header(voucher, h.sign(voucher)))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"
    assert h.state()["lastNonce"] == 0


def test_body_too_large_is_413(h: Harness):
    r = h.post_raw(b'{"task":"echo","input":"' + b"a" * 20_000 + b'"}', "x")
    assert r.status_code == 413
    assert r.json()["error"] == "body_too_large"


# ---------------------------------------------------------------------------
# failures that are not the client's fault
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda h: setattr(h.chain, "agent", None), "agent_not_registered"),
        (lambda h: setattr(h.chain, "unavailable", True), "chain_unavailable"),
    ],
)
def test_infrastructure_failures_are_503_and_never_charge(h: Harness, mutate, code):
    mutate(h)
    r = h.paid_post()
    assert r.status_code == 503
    assert r.json()["error"] == code
    assert h.state()["lastNonce"] == 0


def test_registry_owner_mismatch_is_503(h: Harness):
    h.chain.agent = type(h.chain.agent)(**{**h.chain.agent.__dict__, "owner": Account.create().address})
    r = h.paid_post()
    assert r.status_code == 503 and r.json()["error"] == "agent_registry_mismatch"


def test_registry_signer_mismatch_is_503(h: Harness):
    h.chain.agent = type(h.chain.agent)(**{**h.chain.agent.__dict__, "signer": Account.create().address})
    r = h.paid_post()
    assert r.status_code == 503 and r.json()["error"] == "agent_registry_mismatch"


def test_failed_job_releases_the_voucher(h: Harness, monkeypatch):
    import app.main as main

    body = h.body()
    voucher = h.voucher(body, nonce=1, cumulative=PRICE)
    ticket = h.header(voucher, h.sign(voucher))
    raw = json.dumps(body).encode()

    monkeypatch.setattr(main, "run_task", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = h.post_raw(raw, ticket)
    assert failed.status_code == 500
    assert failed.json()["error"] == "execution_failed"
    assert h.state()["lastNonce"] == 0  # not charged

    monkeypatch.undo()
    retry = h.post_raw(raw, ticket)  # the same voucher is still usable
    assert retry.status_code == 200


def test_failed_job_does_not_roll_back_past_an_earlier_payment(h: Harness, monkeypatch):
    import app.main as main

    assert h.paid_post(h.body(input="ok"), nonce=1, cumulative=1000).status_code == 200
    monkeypatch.setattr(main, "run_task", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    assert h.paid_post(h.body(input="bad"), nonce=2, cumulative=2000).status_code == 500
    state = h.state()
    assert (state["lastNonce"], state["lastCumulativeAmount"]) == (1, "1000")


def test_rate_limit(tmp_path):
    limited = Harness(str(tmp_path / "rl.sqlite3"), rate_limit_per_minute=3)
    codes = [limited.client.post("/v1/execute", json=limited.body()).status_code for _ in range(5)]
    assert codes == [402, 402, 402, 429, 429]
    r = limited.client.post("/v1/execute", json=limited.body())
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    limited.client.close()


# ---------------------------------------------------------------------------
# discovery endpoints
# ---------------------------------------------------------------------------


def test_agent_card(h: Harness):
    r = h.client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["registrations"] == [{"agentId": 1, "agentRegistry": f"eip155:84532:{h.settings.identity_contract}"}]
    assert card["services"][0]["endpoint"].endswith("/v1/execute")
    assert card["amx"]["channelContract"] == h.settings.channel_contract
    assert card["amx"]["signingKey"]["publicKey"] == h.settings.agent_pubkey_hex
    assert "privateKey" not in json.dumps(card) and "agent_private_key" not in json.dumps(card)


def test_health_reports_registry_state(h: Harness):
    assert h.client.get("/health").json() == {
        "status": "ok", "agentId": 1, "network": "base-sepolia", "registry": "ok", "hcs": "disabled",
    }
    h.chain.agent = None
    assert h.client.get("/health").json()["registry"] == "agent_not_registered"
    h.chain.unavailable = True
    assert h.client.get("/health").json()["status"] == "degraded"


def test_channel_status_validates_its_path(h: Harness):
    assert h.client.get("/v1/channels/not-a-channel").status_code == 422
    assert h.state()["minNonce"] == 1


def test_private_key_never_appears_in_settings_repr(h: Harness):
    assert "11" * 32 not in repr(h.settings)


def test_unused_constant_guard():
    assert CHANNEL.startswith("0x")