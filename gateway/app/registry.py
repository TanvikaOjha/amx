"""Read-only on-chain access (web3.py, async).

Covers ERC8004Identity (ownerOf / agentURI / pubKey / signerOf) and the one PaymentChannel
view the gateway needs (getChannel).  Results are cached for a short TTL so that a burst of
requests (or an attacker spraying random channel ids) does not become an RPC burst.

Error model
  * a contract revert on the *existence* check  -> "not found"  (returns None)
  * anything else (network down, timeout, bad RPC reply, unexpected revert) -> ChainUnavailable
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Protocol, TypeVar

from aiohttp import ClientTimeout
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import ContractLogicError

log = logging.getLogger("amx.registry")

STATUS_NONE, STATUS_OPEN, STATUS_CLOSING, STATUS_CLOSED = 0, 1, 2, 3

IDENTITY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "ownerOf",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "agentURI",
        "stateMutability": "view",
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "type": "function",
        "name": "pubKey",
        "stateMutability": "view",
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bytes"}],
    },
    {
        "type": "function",
        "name": "signerOf",
        "stateMutability": "view",
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]

CHANNEL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "getChannel",
        "stateMutability": "view",
        "inputs": [{"name": "channelId", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "payer", "type": "address"},
                    {"name": "status", "type": "uint8"},
                    {"name": "challengePeriod", "type": "uint64"},
                    {"name": "closeDeadline", "type": "uint64"},
                    {"name": "agentId", "type": "uint256"},
                    {"name": "deposit", "type": "uint256"},
                    {"name": "claimed", "type": "uint256"},
                ],
            }
        ],
    }
]


class ChainUnavailable(Exception):
    """The RPC endpoint failed or returned something we cannot interpret."""


@dataclass(frozen=True)
class AgentRecord:
    agent_id: int
    owner: str
    agent_uri: str
    pub_key: bytes
    signer: str  # address derived from pub_key by the contract; zero address when unset


@dataclass(frozen=True)
class ChannelRecord:
    payer: str
    status: int
    challenge_period: int
    close_deadline: int
    agent_id: int
    deposit: int
    claimed: int


class ChainReader(Protocol):
    async def get_agent(self, agent_id: int) -> AgentRecord | None: ...

    async def get_channel(self, channel_id: str) -> ChannelRecord | None: ...

    async def aclose(self) -> None: ...


T = TypeVar("T")


class _TTLCache(Generic[T]):
    def __init__(self, ttl: float, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: dict[Any, tuple[float, T]] = {}

    def get(self, key: Any) -> tuple[bool, T | None]:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return True, hit[1]
        return False, None

    def put(self, key: Any, value: T) -> None:
        if self._ttl <= 0:
            return
        if len(self._data) >= self._max:  # crude bound against cache-filling attacks
            now = time.monotonic()
            self._data = {k: v for k, v in self._data.items() if v[0] > now}
            if len(self._data) >= self._max:
                self._data.clear()
        self._data[key] = (time.monotonic() + self._ttl, value)


class Web3ChainReader:
    def __init__(
        self,
        rpc_url: str,
        identity_address: str,
        channel_address: str,
        *,
        agent_ttl: float = 30.0,
        channel_ttl: float = 5.0,
        timeout: float = 10.0,
    ) -> None:
        self._timeout = timeout
        self._w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": ClientTimeout(total=timeout)}))
        self._identity = self._w3.eth.contract(address=identity_address, abi=IDENTITY_ABI)
        self._channel = self._w3.eth.contract(address=channel_address, abi=CHANNEL_ABI)
        self._agents: _TTLCache[AgentRecord | None] = _TTLCache(agent_ttl)
        self._channels: _TTLCache[ChannelRecord | None] = _TTLCache(channel_ttl)

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run a contract call; map transport failures to ChainUnavailable, let reverts propagate."""
        try:
            return await asyncio.wait_for(fn(), timeout=self._timeout)
        except ContractLogicError:
            raise
        except Exception as exc:  # aiohttp / web3 / timeout / decoding errors
            log.warning("rpc call failed: %s: %s", type(exc).__name__, exc)
            raise ChainUnavailable(f"{type(exc).__name__}: {exc}") from exc

    async def get_agent(self, agent_id: int) -> AgentRecord | None:
        hit, cached = self._agents.get(agent_id)
        if hit:
            return cached

        try:
            owner = await self._call(lambda: self._identity.functions.ownerOf(agent_id).call())
        except ContractLogicError:
            self._agents.put(agent_id, None)  # ERC721NonexistentToken: agent is not registered
            return None

        try:
            uri = await self._call(lambda: self._identity.functions.agentURI(agent_id).call())
            pub_key = await self._call(lambda: self._identity.functions.pubKey(agent_id).call())
            signer = await self._call(lambda: self._identity.functions.signerOf(agent_id).call())
        except ContractLogicError as exc:
            raise ChainUnavailable(f"unexpected revert reading agent {agent_id}: {exc}") from exc

        record = AgentRecord(agent_id=agent_id, owner=owner, agent_uri=uri, pub_key=bytes(pub_key), signer=signer)
        self._agents.put(agent_id, record)
        return record

    async def get_channel(self, channel_id: str) -> ChannelRecord | None:
        hit, cached = self._channels.get(channel_id)
        if hit:
            return cached

        try:
            raw = await self._call(
                lambda: self._channel.functions.getChannel(bytes.fromhex(channel_id[2:])).call()
            )
        except ContractLogicError as exc:
            raise ChainUnavailable(f"unexpected revert reading channel: {exc}") from exc

        payer, status, challenge_period, close_deadline, agent_id, deposit, claimed = raw
        record: ChannelRecord | None
        if status == STATUS_NONE:
            record = None
        else:
            record = ChannelRecord(
                payer=payer,
                status=int(status),
                challenge_period=int(challenge_period),
                close_deadline=int(close_deadline),
                agent_id=int(agent_id),
                deposit=int(deposit),
                claimed=int(claimed),
            )
        self._channels.put(channel_id, record)
        return record

    async def aclose(self) -> None:
        try:
            await self._w3.provider.disconnect()
        except Exception:  # best effort on shutdown
            pass