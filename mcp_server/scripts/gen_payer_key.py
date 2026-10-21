"""Generate a throwaway TESTNET payer keypair.

    cd mcp_server && python scripts/gen_payer_key.py

Prints the PAYER_PRIVATE_KEY line for mcp_server/.env (keep it local; never paste it into chat or commit it)
and the address to fund from a faucet. The same account must open the payment channel (scripts/open_channel.py).
"""

from eth_account import Account

account = Account.create()
print("# --- add to mcp_server/.env (private: stays on this machine) ---------------")
print(f"PAYER_PRIVATE_KEY=0x{bytes(account.key).hex()}")
print()
print("# --- public information (safe to share) --------------------------------------")
print(f"payer address: {account.address}")
print()
print("Next: fund that address with testnet ETH (Base Sepolia faucet), then run scripts/open_channel.py.")