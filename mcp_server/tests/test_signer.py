"""Signer tests: protocol helpers, challenge validation, spend limits, persistence, and the hostile-gateway threat model."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest
from eth_account import Account
from eth_keys import keys as eth_keys
from pydantic import ValidationError

from conftest import CHANNEL_CONTRACT, CHANNEL_ID, PAYER, PAYER_KEY_HEX, challenge_body, make_settings
from signer import (
    Accept,
    CanonicalizationError,
    SignerError,
    SignerSettings,
    VoucherSigner,
    canonical_bytes,
    encode_ticket,
    request_hash,
    select_accept,
    voucher_digest,
    voucher_typed_data,
)

H = "0x" + "cd" * 32


def accept(price: int = 1000, min_nonce: int = 1, min_cumulative: int = 1000) -> Accept:
    return Accept(price=price, min_nonce=min_nonce, min_cumulative=min_cumulative)


def code_of(call) -> str:
    with pytest.raises(SignerError) as exc:
        call()
    return exc.value.code


# ===========================================================================
# protocol helpers
# ===========================================================================


def test_canonical_known_vector_and_hash():
    obj = {"b": 1, "a": [True, None, "é"]}
    assert canonical_bytes(obj) == '{"a":[true,null,"é"],"b":1}'.encode("utf-8")
    assert request_hash(obj) == "0x" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def test_canonical_is_independent_of_key_order():
    assert canonical_bytes({"a": 1, "b": {"y": 1, "x": 2}}) == canonical_bytes({"b": {"x": 2, "y": 1}, "a": 1})


@pytest.mark.parametrize("bad", [{"a": 1.5}, {"a": 2**60}, {1: "x"}, {"a": "\ud800"}, {"a": object()}])
def test_canonical_rejects_what_the_gateway_rejects(bad):
    with pytest.raises(CanonicalizationError):
        canonical_bytes(bad)


def test_independent_eip712_digest_matches_eth_accounts_signer():
    voucher = {
        "channelId": CHANNEL_ID, "agentId": 7, "cumulativeAmount": "123456789012345678901234567890",
        "nonce": 42, "expiry": 1_900_000_000, "requestHash": H,
    }
    signed = Account.sign_typed_data(PAYER.key, full_message=voucher_typed_data(84532, CHANNEL_CONTRACT, voucher))
    raw = bytes(signed.signature)
    sig = eth_keys.Signature(vrs=(raw[64] - 27, int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big")))
    recovered = sig.recover_public_key_from_msg_hash(voucher_digest(84532, CHANNEL_CONTRACT, voucher)).to_checksum_address()
    assert recovered == PAYER.address


def test_ticket_encoding_is_unpadded_base64url_json():
    ticket = encode_ticket({"nonce": 1}, "0xab")
    assert "=" not in ticket and "+" not in ticket and "/" not in ticket
    import base64

    assert json.loads(base64.urlsafe_b64decode(ticket + "==")) == {"voucher": {"nonce": 1}, "signature": "0xab"}


# ===========================================================================
# settings
# ===========================================================================


def test_valid_settings_and_derived_address(tmp_path):
    s = make_settings(tmp_path)
    assert s.payer_address == PAYER.address
    assert s.channel_contract == CHANNEL_CONTRACT


@pytest.mark.parametrize("chain_id", [1, 8453, 10, 137, 0])
def test_only_testnets_are_allowed(tmp_path, chain_id):
    with pytest.raises(ValidationError, match="testnet"):
        make_settings(tmp_path, chain_id=chain_id)


@pytest.mark.parametrize("chain_id", [84532, 11155111, 31337])
def test_allowed_testnets(tmp_path, chain_id):
    assert make_settings(tmp_path, chain_id=chain_id).chain_id == chain_id


def test_budget_has_no_defaults(tmp_path):
    values = dict(
        payer_private_key="0x" + PAYER_KEY_HEX, channel_id=CHANNEL_ID, agent_id=1, chain_id=84532,
        channel_contract=CHANNEL_CONTRACT, state_file=tmp_path / "s.json",
    )
    with pytest.raises(ValidationError) as exc:
        SignerSettings(_env_file=None, **values)
    fields = {e["loc"][0] for e in exc.value.errors()}
    assert {"max_price_per_call", "max_session_spend"} <= fields


def test_session_cap_must_cover_at_least_one_call(tmp_path):
    with pytest.raises(ValidationError, match="MAX_SESSION_SPEND"):
        make_settings(tmp_path, max_price_per_call=5000, max_session_spend=4000)


@pytest.mark.parametrize(
    "url, ok",
    [
        ("http://localhost:8000", True), ("http://127.0.0.1:8000", True), ("https://gw.example.com", True),
        ("http://gw.example.com", False), ("ftp://gw.example.com", False), ("not a url", False),
    ],
)
def test_gateway_url_must_be_https_unless_local(tmp_path, url, ok):
    if ok:
        assert make_settings(tmp_path, gateway_url=url).gateway_url == url
    else:
        with pytest.raises(ValidationError):
            make_settings(tmp_path, gateway_url=url)
    assert make_settings(tmp_path, gateway_url="http://gw.example.com", allow_insecure_http=True)  # explicit opt-in


@pytest.mark.parametrize("key", ["", "0x1234", "zz" * 32, "0x" + "11" * 33])
def test_bad_private_keys_are_rejected(tmp_path, key):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, payer_private_key=key)


@pytest.mark.parametrize("channel_id", ["0x1234", "0x" + "AB" * 32, "ab" * 32, "0x" + "zz" * 32])
def test_channel_id_must_be_lowercase_bytes32(tmp_path, channel_id):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, channel_id=channel_id)


def test_the_private_key_never_appears_in_reprs(tmp_path):
    s = make_settings(tmp_path)
    signer = VoucherSigner(s)
    assert PAYER_KEY_HEX not in repr(s) and PAYER_KEY_HEX not in str(s)
    assert PAYER_KEY_HEX not in repr(signer.prepare(accept(), H))


# ===========================================================================
# challenge validation (the gateway is untrusted)
# ===========================================================================


def test_valid_challenge_is_accepted(tmp_path):
    s = make_settings(tmp_path)
    assert select_accept(challenge_body(3, 3000, 1000), s) == Accept(price=1000, min_nonce=3, min_cumulative=3000)


def test_extra_fields_from_future_gateways_are_tolerated(tmp_path):
    body = challenge_body(extra_field="x")
    body["something_new"] = 1
    assert select_accept(body, make_settings(tmp_path)).price == 1000


def test_contract_address_comparison_ignores_case(tmp_path):
    s = make_settings(tmp_path)
    assert select_accept(challenge_body(channelContract=CHANNEL_CONTRACT.lower()), s)


@pytest.mark.parametrize(
    "override",
    [
        {"scheme": "exact"}, {"asset": "usdc"}, {"network": "base"}, {"agentId": 2},
        {"channelContract": "0x" + "11" * 20}, {"channelContract": "nope"},
    ],
)
def test_options_for_a_different_scheme_asset_network_agent_or_contract_are_refused(tmp_path, override):
    assert code_of(lambda: select_accept(challenge_body(**override), make_settings(tmp_path))) == "challenge_mismatch"


def test_the_matching_option_is_chosen_among_several(tmp_path):
    body = challenge_body(network="base")
    body["accepts"].append(challenge_body(min_nonce=9, min_cumulative=9000)["accepts"][0])
    assert select_accept(body, make_settings(tmp_path)).min_nonce == 9


@pytest.mark.parametrize(
    "override",
    [
        {"pricePerCall": "-1"}, {"pricePerCall": "1.5"}, {"pricePerCall": "abc"}, {"pricePerCall": str(2**256)},
        {"pricePerCall": 1000}, {"minCumulativeAmount": "01"}, {"minNonce": 0}, {"minNonce": 2**60},
    ],
)
def test_malformed_numbers_are_refused(tmp_path, override):
    with pytest.raises(SignerError) as exc:
        select_accept(challenge_body(**override), make_settings(tmp_path))
    assert exc.value.code in ("malformed_challenge", "challenge_mismatch")


@pytest.mark.parametrize("body", [None, [], {}, {"error": "other"}, {"error": "payment_required"}, {"error": "payment_required", "accepts": []}, "text"])
def test_non_challenges_are_refused(tmp_path, body):
    assert code_of(lambda: select_accept(body, make_settings(tmp_path))) == "malformed_challenge"


# ===========================================================================
# spend limits
# ===========================================================================


def test_first_voucher_and_the_next(tmp_path):
    signer = VoucherSigner(make_settings(tmp_path))
    first = signer.prepare(accept(), H, now=1_000)
    assert (first.nonce, first.cumulative, first.delta) == (1, 1000, 1000)
    assert first.voucher["expiry"] == 1_300 and first.voucher["requestHash"] == H
    assert first.voucher["channelId"] == CHANNEL_ID and first.voucher["cumulativeAmount"] == "1000"
    signer.commit(first)
    second = signer.prepare(accept(1000, 2, 2000), H)
    assert (second.nonce, second.cumulative, second.delta) == (2, 2000, 1000)
    assert signer.session_spent == 2000 and signer.session_remaining == 98_000


def test_signatures_are_canonical_and_recover_to_the_payer(tmp_path):
    """Low-s, v in {27,28}: what the gateway and PaymentChannel accept. Many samples, since s is random-looking."""
    signer = VoucherSigner(make_settings(tmp_path, max_session_spend=10**9))
    for i in range(1, 60):
        p = signer.prepare(accept(1000, i, 1000 * i), "0x" + f"{i:064x}")
        raw = bytes.fromhex(p.signature[2:])
        assert len(raw) == 65 and raw[64] in (27, 28)
        assert int.from_bytes(raw[32:64], "big") <= 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0
        signer.commit(p)


def test_price_cap_refuses_before_signing_or_persisting(tmp_path):
    s = make_settings(tmp_path, max_price_per_call=10_000)
    signer = VoucherSigner(s)
    assert code_of(lambda: signer.prepare(accept(price=10_001, min_cumulative=10_001), H)) == "price_exceeds_cap"
    assert not s.state_file.exists() and signer.session_spent == 0
    assert signer.prepare(accept(price=10_000, min_cumulative=10_000), H).delta == 10_000  # exactly the cap is fine


def test_session_cap_stops_spending_at_the_boundary(tmp_path):
    signer = VoucherSigner(make_settings(tmp_path, max_price_per_call=1000, max_session_spend=3000))
    for n in (1, 2, 3):
        signer.commit(signer.prepare(accept(1000, n, 1000 * n), H))
    assert signer.session_remaining == 0
    assert code_of(lambda: signer.prepare(accept(1000, 4, 4000), H)) == "session_cap_exceeded"
    assert signer.session_spent == 3000  # the refusal changed nothing


def test_abort_restores_state_and_budget(tmp_path):
    s = make_settings(tmp_path)
    signer = VoucherSigner(s)
    signer.commit(signer.prepare(accept(), H))
    pending = signer.prepare(accept(1000, 2, 2000), H)
    assert signer.session_spent == 2000
    signer.abort(pending)
    assert signer.session_spent == 1000
    retry = signer.prepare(accept(1000, 2, 2000), H)
    assert (retry.nonce, retry.cumulative) == (2, 2000)  # the aborted nonce is reusable


def test_only_one_payment_can_be_in_flight(tmp_path):
    signer = VoucherSigner(make_settings(tmp_path))
    pending = signer.prepare(accept(), H)
    assert code_of(lambda: signer.prepare(accept(1000, 2, 2000), H)) == "busy"
    signer.commit(pending)
    signer.prepare(accept(1000, 2, 2000), H)


def test_a_gateway_that_forgot_a_released_voucher_does_not_make_us_reuse_a_nonce(tmp_path):
    """After a commit, a LOWER server minimum (e.g. it released a voucher) never lowers our own nonce."""
    signer = VoucherSigner(make_settings(tmp_path))
    signer.commit(_advance(signer, 5))
    p = signer.prepare(accept(1000, 1, 1000), H)
    assert (p.nonce, p.cumulative, p.delta) == (6, 6000, 1000)


def _advance(signer: VoucherSigner, to_nonce: int):
    """Sign vouchers 1..to_nonce, returning the last (uncommitted) one."""
    last = None
    for n in range(1, to_nonce + 1):
        if last is not None:
            signer.commit(last)
        last = signer.prepare(accept(1000, n, 1000 * n), H)
    return last


# ---- hostile / out-of-sync gateway -------------------------------------------------------------------


def test_server_ahead_of_our_record_is_refused_by_default(tmp_path):
    s = make_settings(tmp_path)
    signer = VoucherSigner(s)
    assert code_of(lambda: signer.prepare(accept(1000, 8, 8000), H)) == "state_ahead_of_signer"
    assert not s.state_file.exists()  # nothing signed, nothing persisted


def test_a_gateway_demanding_a_huge_cumulative_cannot_drain_the_channel(tmp_path):
    signer = VoucherSigner(make_settings(tmp_path))
    assert code_of(lambda: signer.prepare(accept(1000, 2, 10**18), H)) == "state_ahead_of_signer"


def test_even_with_resync_enabled_the_session_cap_still_bounds_the_loss(tmp_path):
    s = make_settings(tmp_path, allow_resync=True, max_session_spend=100_000)
    signer = VoucherSigner(s)
    assert code_of(lambda: signer.prepare(accept(1000, 2, 10**18), H)) == "session_cap_exceeded"
    assert not s.state_file.exists()


def test_resync_adopts_a_plausible_server_state_and_charges_it_to_the_session(tmp_path):
    signer = VoucherSigner(make_settings(tmp_path, allow_resync=True, max_session_spend=100_000))
    p = signer.prepare(accept(1000, 8, 8000), H)
    assert (p.nonce, p.cumulative, p.delta) == (8, 8000, 8000)
    assert signer.session_spent == 8000


# ===========================================================================
# persistence
# ===========================================================================


def test_state_survives_a_restart(tmp_path):
    s = make_settings(tmp_path)
    first = VoucherSigner(s)
    first.commit(_advance(first, 3))
    restarted = VoucherSigner(s)
    p = restarted.prepare(accept(1000, 4, 4000), H)
    assert (p.nonce, p.cumulative) == (4, 4000)
    assert restarted.session_spent == 1000  # the session budget is per process, the nonce record is not


def test_state_is_persisted_before_the_ticket_can_be_sent(tmp_path):
    s = make_settings(tmp_path)
    VoucherSigner(s).prepare(accept(), H)  # never committed, never aborted: simulates a crash right after signing
    data = json.loads(s.state_file.read_text())
    assert data["channels"][CHANNEL_ID] == {"last_nonce": 1, "last_cumulative": "1000"}
    resumed = VoucherSigner(s).prepare(accept(), H)  # server still says nonce 1: we skip ahead, never reuse
    assert resumed.nonce == 2 and resumed.cumulative == 2000


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_state_file_is_private_and_written_atomically(tmp_path):
    s = make_settings(tmp_path)
    VoucherSigner(s).prepare(accept(), H)
    assert stat.S_IMODE(os.stat(s.state_file).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_corrupt_state_file_is_an_error_not_a_silent_reset(tmp_path):
    s = make_settings(tmp_path)
    s.state_file.write_text("{ definitely not json")
    signer = VoucherSigner(s)
    assert code_of(lambda: signer.prepare(accept(), H)) == "state_corrupt"
    assert s.state_file.read_text() == "{ definitely not json"  # left untouched for a human to inspect


def test_channels_are_tracked_independently(tmp_path):
    a = make_settings(tmp_path)
    b = make_settings(tmp_path, channel_id="0x" + "ee" * 32)
    VoucherSigner(a).commit(_advance(VoucherSigner(a), 3))
    p = VoucherSigner(b).prepare(accept(), H)
    assert (p.nonce, p.cumulative) == (1, 1000)
    assert VoucherSigner(a).prepare(accept(1000, 4, 4000), H).nonce == 4