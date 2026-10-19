"""Generate a throwaway TESTNET keypair for the agent's signing identity.

Run locally:  python scripts/gen_agent_key.py

Prints the lines to put in gateway/.env (private key stays on your machine) and the 64-byte
public key to register on-chain.  Never paste the private key into chat or commit it.
"""

from eth_account import Account
from eth_keys import keys

account = Account.create()
public_key = keys.PrivateKey(bytes(account.key)).public_key

print("# --- add to gateway/.env ---------------------------------------------")
print(f"AGENT_PRIVATE_KEY={account.key.hex() if account.key.hex().startswith('0x') else '0x' + account.key.hex()}")
print()
print("# --- public information (safe to share) --------------------------------")
print(f"agent signer address : {account.address}")
print(f"agent public key (64B): 0x{public_key.to_bytes().hex()}")
print()
print("Next: register the agent on-chain with that public key (contracts/scripts/register-agent.ts).")