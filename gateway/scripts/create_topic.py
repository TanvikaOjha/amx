"""Create the HCS audit topic and print its TOPIC_ID.

    cd gateway
    python scripts/create_topic.py

Reads OPERATOR_ID, OPERATOR_KEY (+ OPERATOR_KEY_TYPE for raw keys) and HEDERA_NETWORK from gateway/.env.
TESTNETS ONLY: the settings model refuses any network other than testnet / previewnet.

The topic is created with:
  * submit key = admin key = the operator's public key.  Without a submit key ANYONE could post to the topic
    and flood or forge "proofs".  (Reading is always public on Hedera; trust still comes from `gatewaySig`.)
  * Use --open-submit only if you really want a topic anyone can write to.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hedera_logger import HASHSCAN_URLS, HederaSettings, make_sdk_client  # noqa: E402

DEFAULT_MEMO = "Agent-Mesh-X audit log v1"


def create_topic(hs: HederaSettings, memo: str = DEFAULT_MEMO, open_submit: bool = False) -> str:
    """Create a topic and return its id (e.g. '0.0.1234567')."""
    from hiero_sdk_python import TopicCreateTransaction

    client = make_sdk_client(hs)
    try:
        operator_public_key = client.operator_private_key.public_key()
        transaction = TopicCreateTransaction(
            memo=memo,
            admin_key=operator_public_key,
            submit_key=None if open_submit else operator_public_key,
        )
        # validate_status=True: the SDK default (False) would return a receipt even if consensus rejected the transaction.
        receipt = transaction.execute(client, validate_status=True)
        if receipt.topic_id is None:
            raise RuntimeError("receipt did not contain a topic id")
        return str(receipt.topic_id)
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--memo", default=DEFAULT_MEMO, help="topic memo (max 100 bytes)")
    parser.add_argument("--open-submit", action="store_true", help="allow anyone to submit (NOT recommended)")
    args = parser.parse_args()

    try:
        hs = HederaSettings()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if not hs.has_operator:
        print("Set OPERATOR_ID and OPERATOR_KEY in gateway/.env first (a funded TESTNET account).", file=sys.stderr)
        return 2

    print(f"Creating topic on {hs.hedera_network} as operator {hs.operator_id} ...")
    topic_id = create_topic(hs, memo=args.memo, open_submit=args.open_submit)

    print("\n==================== TOPIC CREATED ====================")
    print(f"TOPIC_ID={topic_id}")
    print(f"HashScan: {HASHSCAN_URLS[hs.hedera_network]}/topic/{topic_id}")
    print("\nAdd the TOPIC_ID line to gateway/.env, then restart the gateway.")
    print("=======================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())