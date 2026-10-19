"""Gateway configuration, loaded from environment / gateway/.env (git-ignored)."""

from __future__ import annotations

from functools import lru_cache

from eth_keys import keys as eth_keys
from eth_utils import is_address, to_checksum_address
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- chain (values printed by contracts/scripts/deploy.ts) ---------------
    chain_network: str = Field(default="base-sepolia", min_length=1)
    chain_id: int = Field(default=84532, gt=0)
    rpc_url: str = Field(min_length=1)
    identity_contract: str
    channel_contract: str

    # --- the agent this gateway serves ---------------------------------------
    agent_id: int = Field(ge=1)
    agent_owner_address: str  # expected ownerOf(agent_id); a mismatch means the registry changed under us
    agent_private_key: SecretStr  # signs HCS proofs in Phase 3; its address must equal signerOf(agent_id)

    # --- pricing / acceptance policy ------------------------------------------
    price_per_call: int = Field(default=1000, ge=1)  # wei
    min_accepted_challenge_period: int = Field(default=3600, ge=0)  # seconds

    # --- service ---------------------------------------------------------------
    public_base_url: str = "http://localhost:8000"
    db_path: str = "./amx_gateway.sqlite3"
    rate_limit_per_minute: int = Field(default=60, ge=1)
    trust_proxy_headers: bool = False  # honour X-Forwarded-For only when behind a trusted proxy
    max_body_bytes: int = Field(default=16_384, ge=1)
    max_ticket_bytes: int = Field(default=2_048, ge=1)
    chain_cache_ttl_seconds: float = Field(default=5.0, ge=0)
    registry_cache_ttl_seconds: float = Field(default=30.0, ge=0)
    rpc_timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("identity_contract", "channel_contract", "agent_owner_address")
    @classmethod
    def _checksum(cls, value: str) -> str:
        if not is_address(value):
            raise ValueError("must be a 20-byte hex address")
        return to_checksum_address(value)

    @field_validator("agent_private_key")
    @classmethod
    def _key_shape(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        raw = raw[2:] if raw.startswith("0x") else raw
        if len(raw) != 64:
            raise ValueError("must be a 32-byte hex private key")
        try:
            bytes.fromhex(raw)
        except ValueError as exc:
            raise ValueError("must be a 32-byte hex private key") from exc
        return SecretStr("0x" + raw)

    @model_validator(mode="after")
    def _normalize(self) -> "Settings":
        self.public_base_url = self.public_base_url.rstrip("/")
        return self

    # --- derived values (never log the private key) ---------------------------
    @property
    def _private_key(self) -> eth_keys.PrivateKey:
        return eth_keys.PrivateKey(bytes.fromhex(self.agent_private_key.get_secret_value()[2:]))

    @property
    def agent_signer_address(self) -> str:
        return to_checksum_address(self._private_key.public_key.to_address())

    @property
    def agent_pubkey_hex(self) -> str:
        """64-byte uncompressed secp256k1 public key (x||y), the format ERC8004Identity stores."""
        return "0x" + self._private_key.public_key.to_bytes().hex()

    @property
    def agent_registry_id(self) -> str:
        """CAIP-10 style registry identifier used in the registration file."""
        return f"eip155:{self.chain_id}:{self.identity_contract}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]