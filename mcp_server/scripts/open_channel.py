"""Open an AMX payment channel from the payer account and print the CHANNEL_ID.

    cd mcp_server
    python scripts/open_channel.py --deposit-eth 0.0001            # opens the channel
    python scripts/open_channel.py --deposit-eth 0.0001 --dry-run  # checks everything, sends nothing

Reads PAYER_PRIVATE_KEY, CHANNEL_CONTRACT, CHAIN_ID, AGENT_ID and RPC_URL from mcp_server/.env.
The payer account must already hold testnet ETH (deposit + gas).  Requires `pip install web3` (requirements-dev.txt).

Testnets only: refuses any chain id other than Base Sepolia, Sepolia, or local Hardhat.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402
from pydantic import SecretStr, ValidationError  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402
from web3 import HTTPProvider, Web3  # noqa: E402

from signer import ENV_FILE, TESTNET_CHAIN_IDS  # noqa: E402

CHANNEL_ABI = [
    {
        "type": "function", "name": "openChannel", "stateMutability": "payable",
        "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "challengePeriod", "type": "uint64"}],
        "outputs": [{"name": "channelId", "type": "bytes32"}],
    },
    {
        "type": "function", "name": "minChallengePeriod", "stateMutability": "view",
        "inputs": [], "outputs": [{"name": "", "type": "uint64"}],
    },
]
CHANNEL_OPENED_TOPIC = keccak(text="ChannelOpened(bytes32,address,uint256,uint256,uint64)")


class OpenSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    rpc_url: str = "https://sepolia.base.org"
    payer_private_key: SecretStr
    channel_contract: str
    chain_id: int
    agent_id: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    amount = parser.add_mutually_exclusive_group(required=True)
    amount.add_argument("--deposit-eth", type=str, help="deposit in ETH, e.g. 0.0001")
    amount.add_argument("--deposit-wei", type=int, help="deposit in wei")
    parser.add_argument("--challenge-period", type=int, default=3600, help="unilateral-close window in seconds (default 3600)")
    parser.add_argument("--dry-run", action="store_true", help="validate and print, but send nothing")
    args = parser.parse_args()

    try:
        cfg = OpenSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print("Configuration error (never printing values):", file=sys.stderr)
        for err in exc.errors(include_url=False, include_context=False, include_input=False):
            print(f"  - {'.'.join(str(p).upper() for p in err['loc'])}: {err['msg']}", file=sys.stderr)
        return 2
    if cfg.chain_id not in TESTNET_CHAIN_IDS:
        print(f"Refusing: chain id {cfg.chain_id} is not an allowed testnet {sorted(TESTNET_CHAIN_IDS)}.", file=sys.stderr)
        return 2

    w3 = Web3(HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != cfg.chain_id:
        print(f"Refusing: RPC reports chain id {w3.eth.chain_id}, but CHAIN_ID={cfg.chain_id}.", file=sys.stderr)
        return 2

    deposit = args.deposit_wei if args.deposit_wei is not None else int(w3.to_wei(args.deposit_eth, "ether"))
    if deposit <= 0:
        print("Refusing: deposit must be positive.", file=sys.stderr)
        return 2

    payer = Account.from_key(cfg.payer_private_key.get_secret_value())
    contract_address = to_checksum_address(cfg.channel_contract)
    if not w3.eth.get_code(contract_address):
        print(f"Refusing: no contract at {contract_address} on chain {cfg.chain_id}.", file=sys.stderr)
        return 2
    contract = w3.eth.contract(address=contract_address, abi=CHANNEL_ABI)

    minimum = contract.functions.minChallengePeriod().call()
    if args.challenge_period < minimum:
        print(f"Refusing: --challenge-period {args.challenge_period} is below the contract minimum {minimum}.", file=sys.stderr)
        return 2

    balance = w3.eth.get_balance(payer.address)
    print(f"network     : chain {cfg.chain_id} ({TESTNET_CHAIN_IDS[cfg.chain_id]})")
    print(f"payer       : {payer.address}  (balance {w3.from_wei(balance, 'ether')} ETH)")
    print(f"contract    : {contract_address}")
    print(f"agent id    : {cfg.agent_id}")
    print(f"deposit     : {deposit} wei ({w3.from_wei(deposit, 'ether')} ETH)")
    print(f"challenge   : {args.challenge_period}s")
    if balance <= deposit:
        print("Refusing: the payer balance does not cover the deposit plus gas. Fund the payer address first.", file=sys.stderr)
        return 2

    open_fn = contract.functions.openChannel(cfg.agent_id, args.challenge_period)
    try:
        expected = "0x" + bytes(open_fn.call({"from": payer.address, "value": deposit})).hex()  # also catches UnknownAgent etc.
    except Exception as exc:
        print(f"Refusing: the contract would reject this call ({type(exc).__name__}). Is AGENT_ID registered?", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"\nDRY RUN OK. Would open channel {expected} (id is predicted from the current channel counter).")
        return 0

    tx = open_fn.build_transaction({"from": payer.address, "value": deposit, "nonce": w3.eth.get_transaction_count(payer.address)})
    signed = payer.sign_transaction(tx)
    receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.raw_transaction), timeout=180)
    if receipt.status != 1:
        print("Transaction reverted.", file=sys.stderr)
        return 1

    channel_id = None
    for entry in receipt.logs:
        topics = [bytes(t) for t in entry["topics"]]
        if entry["address"] == contract_address and topics and topics[0] == CHANNEL_OPENED_TOPIC:
            channel_id = "0x" + topics[1].hex()
    if channel_id is None:
        print("Opened, but no ChannelOpened event was found in the receipt.", file=sys.stderr)
        return 1

    print("\n==================== CHANNEL OPENED ====================")
    print(f"CHANNEL_ID={channel_id}")
    print(f"tx: {receipt.transactionHash.hex() if hasattr(receipt.transactionHash, 'hex') else receipt.transactionHash}")
    print("\nAdd the CHANNEL_ID line to mcp_server/.env, then start the MCP server.")
    print("========================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())