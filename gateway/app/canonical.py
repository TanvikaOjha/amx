from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from eth_abi import encode as abi_encode
from eth_keys import keys as eth_keys
from eth_utils import keccak, to_checksum_address

MAX_SAFE_INT = 2**53 - 1
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_HALF_N = SECP256K1_N // 2



class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented in AMX canonical JSON."""


def _validate(obj: Any, depth: int = 0) -> None:
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
            _validate(item, depth + 1)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
            _validate(value, depth + 1)
        return
    raise CanonicalizationError(f"unsupported type: {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    """Canonical UTF-8 encoding: sorted keys, no whitespace, no ASCII escaping."""
    _validate(obj)
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:  # e.g. lone surrogates
        raise CanonicalizationError("string is not valid UTF-8") from exc


def sha256_hex(data: bytes) -> str:
    """0x-prefixed lowercase sha256 (fits a bytes32)."""
    return "0x" + hashlib.sha256(data).hexdigest()


def canonical_hash(obj: Any) -> str:
    return sha256_hex(canonical_bytes(obj))


def request_hash(body: Any) -> str:
    """sha256 of the canonical request body; this is what a voucher's requestHash must equal."""
    return canonical_hash(body)


def _reject_constant(name: str) -> Any:
    raise CanonicalizationError(f"invalid JSON constant: {name}")


def _reject_float(text: str) -> Any:
    raise CanonicalizationError(f"floats are not allowed: {text}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CanonicalizationError(f"duplicate object key: {key!r}")
        out[key] = value
    return out


def loads_strict(raw: bytes | str) -> Any:
    """json.loads that rejects NaN/Infinity, floats, and duplicate keys (hash ambiguity)."""
    try:
        return json.loads(
            raw,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            object_pairs_hook=_no_duplicate_keys,
        )
    except CanonicalizationError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise CanonicalizationError(f"invalid JSON: {exc}") from exc



DOMAIN_NAME = "AMXPaymentChannel"
DOMAIN_VERSION = "1"

VOUCHER_TYPE_STRING = (
    "Voucher(bytes32 channelId,uint256 agentId,uint256 cumulativeAmount,"
    "uint64 nonce,uint64 expiry,bytes32 requestHash)"
)
DOMAIN_TYPE_STRING = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
VOUCHER_TYPEHASH = keccak(text=VOUCHER_TYPE_STRING)
DOMAIN_TYPEHASH = keccak(text=DOMAIN_TYPE_STRING)


class InvalidSignatureError(ValueError):
    """Signature is malformed, malleable, or does not recover to an address."""


@dataclass(frozen=True)
class VoucherDomain:
    chain_id: int
    verifying_contract: str
    name: str = DOMAIN_NAME
    version: str = DOMAIN_VERSION


def _hex_to_bytes32(value: str) -> bytes:
    raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    if len(raw) != 32:
        raise ValueError("expected 32 bytes")
    return raw


def domain_separator(domain: VoucherDomain) -> bytes:
    return keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPEHASH,
                keccak(text=domain.name),
                keccak(text=domain.version),
                domain.chain_id,
                to_checksum_address(domain.verifying_contract),
            ],
        )
    )


def voucher_struct_hash(v: Mapping[str, Any]) -> bytes:
    return keccak(
        abi_encode(
            ["bytes32", "bytes32", "uint256", "uint256", "uint64", "uint64", "bytes32"],
            [
                VOUCHER_TYPEHASH,
                _hex_to_bytes32(v["channelId"]),
                int(v["agentId"]),
                int(v["cumulativeAmount"]),
                int(v["nonce"]),
                int(v["expiry"]),
                _hex_to_bytes32(v["requestHash"]),
            ],
        )
    )


def voucher_digest(domain: VoucherDomain, v: Mapping[str, Any]) -> bytes:
    """EIP-712 digest; equals PaymentChannel.voucherDigest(v) on-chain."""
    return keccak(b"\x19\x01" + domain_separator(domain) + voucher_struct_hash(v))


def voucher_typed_data(domain: VoucherDomain, v: Mapping[str, Any]) -> dict[str, Any]:
    """Full EIP-712 message for signers (eth_account.Account.sign_typed_data(full_message=...))."""
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
            "name": domain.name,
            "version": domain.version,
            "chainId": domain.chain_id,
            "verifyingContract": to_checksum_address(domain.verifying_contract),
        },
        "message": {
            "channelId": v["channelId"],
            "agentId": int(v["agentId"]),
            "cumulativeAmount": int(v["cumulativeAmount"]),
            "nonce": int(v["nonce"]),
            "expiry": int(v["expiry"]),
            "requestHash": v["requestHash"],
        },
    }


def recover_voucher_signer(domain: VoucherDomain, v: Mapping[str, Any], signature_hex: str) -> str:
    """Recover the checksummed signer of a voucher.

    Mirrors what OpenZeppelin's ECDSA.recover accepts on-chain, so anything this function
    accepts is also claimable by PaymentChannel: 65 bytes, v in {27, 28}, and *low-s* only.
    A high-s signature is the malleable twin of a valid one and would revert on-chain.
    """
    try:
        raw = bytes.fromhex(signature_hex[2:] if signature_hex.startswith("0x") else signature_hex)
    except ValueError as exc:
        raise InvalidSignatureError("signature is not hex") from exc
    if len(raw) != 65:
        raise InvalidSignatureError("signature must be 65 bytes")

    r = int.from_bytes(raw[0:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    recovery = raw[64]
    if recovery not in (27, 28):
        raise InvalidSignatureError("v must be 27 or 28")
    if not (0 < r < SECP256K1_N) or not (0 < s <= SECP256K1_HALF_N):
        raise InvalidSignatureError("r/s out of range (high-s signatures are rejected)")

    try:
        signature = eth_keys.Signature(vrs=(recovery - 27, r, s))
        public_key = signature.recover_public_key_from_msg_hash(voucher_digest(domain, v))
        return to_checksum_address(public_key.to_address())
    except Exception as exc:  # eth_keys raises several exception types for bad curve points
        raise InvalidSignatureError("signature does not recover") from exc

