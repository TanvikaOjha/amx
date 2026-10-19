import hashlib

import pytest
from eth_account import Account

from app.canonical import (
    CanonicalizationError,
    InvalidSignatureError,
    VoucherDomain,
    canonical_bytes,
    canonical_hash,
    loads_strict,
    recover_voucher_signer,
    request_hash,
    voucher_typed_data,
)


def test_known_vector():
    obj = {"b": 1, "a": [True, None, "é"]}
    assert canonical_bytes(obj) == '{"a":[true,null,"é"],"b":1}'.encode("utf-8")
    assert request_hash(obj) == "0x" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def test_key_order_and_whitespace_do_not_change_the_hash():
    a = loads_strict('{"task": "echo",\n  "input": "x", "params": {"b": 1, "a": 2}}')
    b = loads_strict('{"params":{"a":2,"b":1},"input":"x","task":"echo"}')
    assert canonical_hash(a) == canonical_hash(b)


def test_bool_and_int_hash_differently():
    assert canonical_hash({"v": True}) != canonical_hash({"v": 1})


@pytest.mark.parametrize(
    "raw",
    ['{"a": 1.5}', '{"a": 1e3}', '{"a": NaN}', '{"a": Infinity}', '{"a": 1, "a": 2}', "{not json", '{"a": 9007199254740993}'],
)
def test_loads_strict_rejects_ambiguous_input(raw):
    with pytest.raises(CanonicalizationError):
        canonical_bytes(loads_strict(raw))


def test_floats_and_non_string_keys_rejected_when_building():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"a": 1.0})
    with pytest.raises(CanonicalizationError):
        canonical_bytes({1: "a"})


def test_lone_surrogate_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"a": "\ud800"})


def test_eip712_digest_matches_eth_account_and_roundtrips():
    """Our hand-rolled digest must agree with eth_account's independent EIP-712 implementation."""
    domain = VoucherDomain(chain_id=84532, verifying_contract="0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512")
    voucher = {
        "channelId": "0x" + "ab" * 32,
        "agentId": 7,
        "cumulativeAmount": "123456789012345678901234567890",
        "nonce": 42,
        "expiry": 1_900_000_000,
        "requestHash": "0x" + "cd" * 32,
    }
    payer = Account.create()
    signed = Account.sign_typed_data(payer.key, full_message=voucher_typed_data(domain, voucher))
    assert recover_voucher_signer(domain, voucher, "0x" + bytes(signed.signature).hex()) == payer.address


def test_recover_rejects_malformed_signatures():
    domain = VoucherDomain(chain_id=1, verifying_contract="0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512")
    voucher = {
        "channelId": "0x" + "00" * 32, "agentId": 1, "cumulativeAmount": "1", "nonce": 1, "expiry": 1,
        "requestHash": "0x" + "00" * 32,
    }
    for bad in ["0x", "0x" + "00" * 65, "0x" + "ff" * 65, "zz", "0x" + "11" * 64]:
        with pytest.raises(InvalidSignatureError):
            recover_voucher_signer(domain, voucher, bad)