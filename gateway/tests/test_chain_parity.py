"""Python <-> Solidity parity, against the REAL contracts on a local Hardhat node.

Skipped unless a node is available.  To run:

    cd contracts && npx hardhat compile && npx hardhat node          # terminal 1
    cd gateway   && AMX_TEST_RPC_URL=http://127.0.0.1:8545 pytest tests/test_chain_parity.py -v   # terminal 2

What it proves
  1. Our EIP-712 digest equals PaymentChannel.voucherDigest() byte for byte.
  2. registry.py reads agents / channels correctly (including "not found").
  3. The full gateway accepts a voucher against real chain state.
  4. The voucher the gateway stored is accepted by PaymentChannel.close() - i.e. what we
     verify off-chain is exactly what the contract will pay out on.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from web3 import HTTPProvider, Web3

from app.canonical import VoucherDomain, request_hash, voucher_digest, voucher_typed_data
from app.main import create_app
from app.registry import STATUS_OPEN, Web3ChainReader
from conftest import make_settings

RPC = os.environ.get("AMX_TEST_RPC_URL")
ARTIFACTS = Path(__file__).resolve().parents[2] / "contracts" / "artifacts" / "contracts"

pytestmark = pytest.mark.skipif(
    not RPC or not ARTIFACTS.exists(),
    reason="set AMX_TEST_RPC_URL and run `npx hardhat compile` in contracts/ to enable on-chain parity tests",
)


def _artifact(name: str) -> dict:
    return json.loads((ARTIFACTS / f"{name}.sol" / f"{name}.json").read_text())


def _voucher_tuple(v: dict) -> tuple:
    return (
        bytes.fromhex(v["channelId"][2:]),
        int(v["agentId"]),
        int(v["cumulativeAmount"]),
        int(v["nonce"]),
        int(v["expiry"]),
        bytes.fromhex(v["requestHash"][2:]),
    )


class Env:
    """A deployed system: identity + channel contracts, a registered agent, and one funded channel."""

    def __init__(self) -> None:
        self.w3 = Web3(HTTPProvider(RPC))
        accounts = self.w3.eth.accounts
        self.deployer, self.owner, self.funder = accounts[0], accounts[1], accounts[2]

        identity_art, channel_art = _artifact("ERC8004Identity"), _artifact("PaymentChannel")
        self.identity = self._deploy(identity_art, [])
        self.channel = self._deploy(channel_art, [self.identity.address, 60])

        # Agent: its signing key is registered on-chain as `pubKey` (64 raw bytes).
        self.agent_account = Account.create()
        from eth_keys import keys

        self.agent_pubkey = keys.PrivateKey(bytes(self.agent_account.key)).public_key.to_bytes()
        register = self.identity.get_function_by_signature("register(string,(string,bytes)[])")
        self._send(register("https://agent.test/.well-known/agent-card.json", [("pubKey", self.agent_pubkey)]), self.owner)
        self.agent_id = 1

        # Payer: a fresh key, funded, who opens a 1-ETH channel with a 1h challenge window.
        self.payer = Account.create()
        self.w3.eth.send_transaction({"from": self.funder, "to": self.payer.address, "value": 2 * 10**18})
        open_fn = self.channel.functions.openChannel(self.agent_id, 3600)
        self.channel_id = "0x" + open_fn.call({"from": self.payer.address, "value": 10**18}).hex()
        tx = open_fn.build_transaction(
            {
                "from": self.payer.address,
                "value": 10**18,
                "nonce": self.w3.eth.get_transaction_count(self.payer.address),
            }
        )
        signed = self.payer.sign_transaction(tx)
        self.w3.eth.wait_for_transaction_receipt(self.w3.eth.send_raw_transaction(signed.raw_transaction))

        self.chain_id = self.w3.eth.chain_id
        self.domain = VoucherDomain(chain_id=self.chain_id, verifying_contract=self.channel.address)

    def _deploy(self, artifact: dict, args: list):
        factory = self.w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])
        tx_hash = factory.constructor(*args).transact({"from": self.deployer})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return self.w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])

    def _send(self, fn, sender: str):
        receipt = self.w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender}))
        assert receipt.status == 1
        return receipt

    def sign(self, voucher: dict) -> str:
        signed = Account.sign_typed_data(self.payer.key, full_message=voucher_typed_data(self.domain, voucher))
        return "0x" + bytes(signed.signature).hex()

    def voucher(self, body: dict, nonce: int, cumulative: int) -> dict:
        return {
            "channelId": self.channel_id,
            "agentId": self.agent_id,
            "cumulativeAmount": str(cumulative),
            "nonce": nonce,
            "expiry": int(time.time()) + 600,
            "requestHash": request_hash(body),
        }


@pytest.fixture(scope="module")
def env() -> Env:
    return Env()


def test_eip712_digest_matches_the_contract(env: Env):
    voucher = env.voucher({"task": "echo", "input": "x"}, nonce=3, cumulative=3000)
    on_chain = env.channel.functions.voucherDigest(_voucher_tuple(voucher)).call()
    assert on_chain == voucher_digest(env.domain, voucher)
    assert env.channel.functions.domainSeparator().call().hex()  # domain is queryable


def test_registry_reads(env: Env):
    async def go():
        reader = Web3ChainReader(RPC, env.identity.address, env.channel.address, agent_ttl=0, channel_ttl=0)
        try:
            agent = await reader.get_agent(env.agent_id)
            assert agent is not None
            assert agent.owner == env.owner
            assert agent.agent_uri.endswith("/.well-known/agent-card.json")
            assert agent.pub_key == env.agent_pubkey
            assert agent.signer == env.agent_account.address  # derived on-chain from pubKey

            assert await reader.get_agent(999) is None  # ERC721NonexistentToken -> not registered

            channel = await reader.get_channel(env.channel_id)
            assert channel is not None
            assert (channel.payer, channel.status, channel.agent_id) == (env.payer.address, STATUS_OPEN, env.agent_id)
            assert (channel.deposit, channel.challenge_period) == (10**18, 3600)

            assert await reader.get_channel("0x" + "00" * 32) is None
        finally:
            await reader.aclose()

    asyncio.run(go())


def test_unreachable_rpc_is_chain_unavailable():
    from app.registry import ChainUnavailable

    async def go():
        reader = Web3ChainReader("http://127.0.0.1:1", "0x" + "11" * 20, "0x" + "22" * 20, timeout=2)
        try:
            with pytest.raises(ChainUnavailable):
                await reader.get_agent(1)
        finally:
            await reader.aclose()

    asyncio.run(go())


def test_gateway_accepts_a_voucher_and_the_contract_pays_it_out(env: Env, tmp_path):
    settings = make_settings(
        str(tmp_path / "ledger.sqlite3"),
        rpc_url=RPC,
        chain_id=env.chain_id,
        identity_contract=env.identity.address,
        channel_contract=env.channel.address,
        agent_owner_address=env.owner,
        agent_private_key="0x" + bytes(env.agent_account.key).hex(),
        price_per_call=1000,
    )

    def post(client, body, nonce, cumulative):
        voucher = env.voucher(body, nonce, cumulative)
        header = __import__("base64").urlsafe_b64encode(
            json.dumps({"voucher": voucher, "signature": env.sign(voucher)}).encode()
        ).decode().rstrip("=")
        return client.post("/v1/execute", json=body, headers={"X-402-Payment-Ticket": header})

    with TestClient(create_app(settings)) as client:  # real Web3ChainReader, real chain
        assert client.get("/health").json()["registry"] == "ok"

        first = post(client, {"task": "echo", "input": "one"}, nonce=1, cumulative=1000)
        assert first.status_code == 200, first.text
        second = post(client, {"task": "sha256", "input": "two"}, nonce=2, cumulative=2000)
        assert second.status_code == 200, second.text

        replayed = post(client, {"task": "echo", "input": "three"}, nonce=2, cumulative=3000)
        assert replayed.status_code == 402 and replayed.json()["reason"] == "nonce_not_increasing"

        over = post(client, {"task": "echo", "input": "huge"}, nonce=3, cumulative=2 * 10**18)
        assert over.status_code == 402 and over.json()["reason"] == "exceeds_deposit"

        # Settlement path: the payee presents the newest voucher the gateway stored.
        stored = client.app.state.amx.ledger.latest_voucher(env.channel_id)
        assert stored is not None
        voucher, signature = stored
        assert (voucher["nonce"], voucher["cumulativeAmount"]) == (2, "2000")

    close = env.channel.functions.close(_voucher_tuple(voucher), bytes.fromhex(signature[2:]))
    receipt = env._send(close, env.owner)  # reverts (and fails this test) if the contract rejects our signature
    assert receipt.status == 1
    assert env.channel.functions.pending(env.owner).call() == 2000
    assert env.channel.functions.pending(env.payer.address).call() == 10**18 - 2000