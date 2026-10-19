"""Verify an AMX audit proof on the Hedera mirror node.

    python scripts/verify_hcs_message.py 0.0.1234567 42
    python scripts/verify_hcs_message.py 0.0.1234567 42 --signer 0xAgentSignerAddress

Fetches message #<sequence> of the topic, base64-decodes it, checks it fits HCS's 1024-byte limit,
parses the proof, and verifies `gatewaySig`.  If --signer is omitted the gateway's own agent address
is used when gateway/.env is loadable; otherwise the recovered signer is printed for you to compare
with `signerOf(agentId)` on the identity contract.

Exit status 0 = found and valid, 1 = not found / invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hedera_logger import (  # noqa: E402
    HASHSCAN_URLS,
    HCS_MAX_MESSAGE_BYTES,
    MIRROR_BASE_URLS,
    ProofError,
    decode_mirror_message,
    mirror_message_url,
    verify_proof,
)


def fetch_message(network: str, topic_id: str, sequence_number: int, wait_seconds: float = 30.0) -> dict[str, Any]:
    """GET the mirror-node record, polling while it is 404 (mirror nodes lag consensus by a few seconds)."""
    url = mirror_message_url(network, topic_id, sequence_number)
    deadline = time.monotonic() + wait_seconds
    last = "no response"
    with httpx.Client(timeout=10.0) as client:
        while True:
            try:
                response = client.get(url)
                if response.status_code == 200:
                    return response.json()
                last = f"HTTP {response.status_code}"
                if response.status_code != 404:
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise TimeoutError(f"message not available from {url} ({last})")
            time.sleep(2.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("topic_id")
    parser.add_argument("sequence_number", type=int)
    parser.add_argument("--network", choices=sorted(MIRROR_BASE_URLS), default="testnet")
    parser.add_argument("--signer", help="expected agent signer address (0x...)")
    parser.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the mirror node (default 30)")
    args = parser.parse_args()

    expected = args.signer
    if expected is None:
        try:
            from app.config import Settings

            expected = Settings().agent_signer_address
            print(f"(using the gateway's configured agent address as the expected signer: {expected})")
        except Exception:
            expected = None

    try:
        record = fetch_message(args.network, args.topic_id, args.sequence_number, args.wait)
        raw = decode_mirror_message(record)
    except (TimeoutError, ProofError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"topic              : {record.get('topic_id')}")
    print(f"sequence number    : {record.get('sequence_number')}")
    print(f"consensus timestamp: {record.get('consensus_timestamp')}")
    print(f"payer account      : {record.get('payer_account_id')}")
    print(f"message size       : {len(raw)} bytes (HCS limit {HCS_MAX_MESSAGE_BYTES})")
    print(f"HashScan           : {HASHSCAN_URLS[args.network]}/topic/{args.topic_id}")

    try:
        proof = json.loads(raw)
        print("proof              :", json.dumps(proof, indent=2).replace("\n", "\n                      "))
        signer = verify_proof(proof, expected)
    except (ValueError, ProofError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"\nOK: gatewaySig valid; signed by {signer}" + (" (matches expected)" if expected else " (no expected signer given: compare with signerOf(agentId))"))
    if any(key in proof for key in ("input", "output", "task")):
        print("WARNING: proof contains task content, which must never be on HCS", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())