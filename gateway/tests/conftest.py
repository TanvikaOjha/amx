"""Shared test harness: a gateway wired to an in-memory fake chain and a temp SQLite ledger."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app.canonical import SECP256K1_N, VoucherDomain, request_hash, voucher_typed_data
from app.config import Settings
from app.main import create_app
from app.registry import STATUS_OPEN, AgentRecord, ChainUnavailable, ChannelRecord

IDENTITY = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
CHANNEL = "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512"
OWNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
AGENT_KEY = "0x" + "11" * 32
CHAIN_ID = 84532
PRICE = 1000
DEPOSIT = 10**6


@dataclass
class FakeChain:
    """Implements the ChainReader protocol with dictionaries; counts calls to assert caching/short-circuiting."""

    agent: AgentRecord | None = None
    channels: dict[str, ChannelRecord] = field(default_factory=dict)
    unavailable: bool = False
    calls: int = 0

    async def get_agent(self, agent_id: int) -> AgentRecord | None:
        self.calls += 1
        if self.unavailable:
            raise ChainUnavailable("rpc down")
        return self.agent

    async def get_channel(self, channel_id: str) -> ChannelRecord | None:
        self.calls += 1
        if self.unavailable:
            raise ChainUnavailable("rpc down")
        return self.channels.get(channel_id)

    async def aclose(self) -> None:
        return None


def make_settings(db_path: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        rpc_url="http://fake.invalid",
        chain_network="base-sepolia",
        chain_id=CHAIN_ID,
        identity_contract=IDENTITY,
        channel_contract=CHANNEL,
        agent_id=1,
        agent_owner_address=OWNER,
        agent_private_key=AGENT_KEY,
        price_per_call=PRICE,
        min_accepted_challenge_period=3600,
        db_path=db_path,
        rate_limit_per_minute=10_000,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class Harness:
    def __init__(self, db_path: str, **settings_overrides: Any) -> None:
        self.settings = make_settings(db_path, **settings_overrides)
        self.payer = Account.create()
        self.channel_id = "0x" + os.urandom(32).hex()
        self.domain = VoucherDomain(chain_id=CHAIN_ID, verifying_contract=CHANNEL)
        self.chain = FakeChain(
            agent=AgentRecord(
                agent_id=1,
                owner=OWNER,
                agent_uri="https://agent.example/.well-known/agent-card.json",
                pub_key=bytes.fromhex(self.settings.agent_pubkey_hex[2:]),
                signer=self.settings.agent_signer_address,
            ),
            channels={self.channel_id: self.open_channel_record()},
        )
        self.db_path = db_path
        self.app = create_app(self.settings, chain=self.chain)
        self.client = TestClient(self.app)

    # -- on-chain state helpers ----------------------------------------------------
    def open_channel_record(self, **overrides: Any) -> ChannelRecord:
        values: dict[str, Any] = dict(
            payer=self.payer.address,
            status=STATUS_OPEN,
            challenge_period=3600,
            close_deadline=0,
            agent_id=1,
            deposit=DEPOSIT,
            claimed=0,
        )
        values.update(overrides)
        return ChannelRecord(**values)

    def set_channel(self, **overrides: Any) -> None:
        self.chain.channels[self.channel_id] = self.open_channel_record(**overrides)

    # -- request building ----------------------------------------------------------
    @staticmethod
    def body(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"task": "echo", "input": "hello mesh"}
        body.update(overrides)
        return body

    def voucher(self, body: dict[str, Any], *, nonce: int = 1, cumulative: int = PRICE, **overrides: Any) -> dict[str, Any]:
        voucher: dict[str, Any] = {
            "channelId": self.channel_id,
            "agentId": 1,
            "cumulativeAmount": str(cumulative),
            "nonce": nonce,
            "expiry": int(time.time()) + 300,
            "requestHash": request_hash(body),
        }
        voucher.update(overrides)
        return voucher

    def sign(self, voucher: dict[str, Any], account: Any = None) -> str:
        account = account or self.payer
        message = voucher_typed_data(self.domain, voucher)
        signed = Account.sign_typed_data(account.key, full_message=message)
        return "0x" + bytes(signed.signature).hex()

    @staticmethod
    def high_s(signature: str) -> str:
        """The malleable twin of a valid signature: (r, N - s, flipped v)."""
        raw = bytes.fromhex(signature[2:])
        r, s, v = raw[:32], int.from_bytes(raw[32:64], "big"), raw[64]
        return "0x" + (r + (SECP256K1_N - s).to_bytes(32, "big") + bytes([55 - v])).hex()

    @staticmethod
    def header(voucher: dict[str, Any], signature: str) -> str:
        payload = json.dumps({"voucher": voucher, "signature": signature}).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    def post_raw(self, raw: bytes, ticket: str | None = None, extra: dict[str, str] | None = None):
        headers = {"content-type": "application/json", **(extra or {})}
        if ticket is not None:
            headers["X-402-Payment-Ticket"] = ticket
        return self.client.post("/v1/execute", content=raw, headers=headers)

    def paid_post(self, body: dict[str, Any] | None = None, *, nonce: int = 1, cumulative: int = PRICE, **voucher_overrides: Any):
        """POST a body with a correctly signed ticket.  Wire formatting is deliberately NOT canonical."""
        body = body if body is not None else self.body()
        voucher = self.voucher(body, nonce=nonce, cumulative=cumulative, **voucher_overrides)
        ticket = self.header(voucher, self.sign(voucher))
        return self.post_raw(json.dumps(body, indent=2).encode(), ticket)

    def state(self) -> dict[str, Any]:
        return self.client.get(f"/v1/channels/{self.channel_id}").json()


@pytest.fixture
def h(tmp_path):
    harness = Harness(str(tmp_path / "ledger.sqlite3"))
    yield harness
    harness.client.close()