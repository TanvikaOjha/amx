# AMX — Agent-Mesh-X

AMX is a payment-enabled agent execution stack. It connects a web interface and an MCP client to an HTTP gateway that advertises agent capabilities, challenges unpaid requests with HTTP **402 Payment Required**, and verifies signed payment-channel vouchers before executing a task.

> **Status:** Development project. Use local networks and test accounts while evaluating it. Review the configuration and contracts carefully before handling real funds.

## Architecture

- **Frontend** — Next.js, React, TypeScript, wagmi, RainbowKit and viem. Browse agents and payment channels, connect a wallet, and submit tasks through the gateway.
- **Gateway** — FastAPI service. Exposes the execution endpoint, serves an agent card, reads agent/channel state from the chain, validates payment tickets, applies request limits and maintains a local ledger.
- **MCP server** — Exposes the `execute_agent_task` tool to MCP-compatible clients. It probes the gateway for a 402 challenge, checks the offered payment option against local spending limits, signs an EIP-712 voucher, then makes one paid attempt.
- **Smart contracts** — Solidity contracts managed with Hardhat 3. The repository includes deployment scripts and network configuration for local development and testnets.
- **Audit trail** — The gateway/MCP flow can return a Hedera Consensus Service (HCS) proof when configured.

## Repository layout

```text
.
├── contracts/    # Solidity contracts, Hardhat config, scripts and tests
├── frontend/     # Next.js web application
├── gateway/      # FastAPI execution and payment-verification gateway
└── mcp_server/   # MCP tool server and voucher signer
```

## How the payment flow works

1. The client sends a task request to the gateway without a payment ticket.
2. The gateway responds with **402** and an AMX payment challenge describing acceptable payment options.
3. The MCP client validates the challenge against its configured limits and signs an EIP-712 payment voucher locally.
4. The client retries the same request with the signed ticket and payment-channel hint.
5. The gateway validates the ticket, request hash, agent, expiry, signature and channel state before reserving payment and executing the task.
6. The client receives the task result and, when configured, audit-proof information.

The MCP server intentionally limits a call to the challenge request and a single paid attempt; it does not keep retrying a failed payment.

## Prerequisites

Install the tools required for the parts you want to run:

- Node.js and npm (frontend and contracts)
- Python 3.11+ recommended (gateway and MCP server)
- An RPC endpoint for the selected EVM network
- Test wallets and deployed contract addresses for an end-to-end blockchain flow
- Optional: a configured Hedera account/topic if you want HCS audit logging

## Local development

### 1. Smart contracts

```bash
cd contracts
npm install
npx hardhat compile
npx hardhat test
```

For local integration tests, start a local Hardhat node in a separate terminal when needed:

```bash
cd contracts
npx hardhat node
```

Deployment scripts and network-specific settings live in `contracts/`. Review the scripts and configure the required secrets and RPC variables before deploying to a testnet. Never commit private keys or populated `.env` files.

### 2. Gateway

```bash
cd gateway
python -m venv .venv
# Linux/macOS/WSL
source .venv/bin/activate
pip install -r requirements.txt
```

Configure the gateway's environment using the settings defined in `gateway/app/config.py` and the example/configuration files provided in the repository. Set the RPC URL, network and chain/contract configuration, agent identity and signer details before running a real chain-backed flow.

Start the API from the `gateway/` directory:

```bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

The gateway's execution endpoint is `POST /v1/execute`. It also exposes health/status and agent-card routes; see `gateway/app/main.py` for the authoritative route definitions.

### 3. MCP server

```bash
cd mcp_server
python -m venv .venv
# Linux/macOS/WSL
source .venv/bin/activate
pip install -r requirements.txt
```

Configure the MCP server's environment according to `mcp_server/signer.py` and the settings used by `mcp_server/server.py`, including the gateway URL, chain/channel details and payer signing key. Keep the key local and never expose it to the frontend or commit it.

Run with stdio transport:

```bash
python server.py
```

An MCP-compatible host (for example, a client that supports local stdio MCP servers) can launch this command using its MCP server configuration. For protocol debugging, the server source documents an MCP Inspector command.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open the local URL printed by Next.js. Configure the frontend's public environment variables and gateway proxy settings as described in `frontend/lib/constants.ts` and `frontend/next.config.mjs`. The frontend expects the gateway to be available for live task execution.

Useful scripts:

```bash
npm run dev
npm run build
npm run start
npm run lint
```

## Built-in gateway tasks

The gateway currently includes deterministic tasks suitable for exercising the execution path:

- `echo` — returns the input text
- `sha256` — returns the SHA-256 digest of the input
- `word_count` — returns the number of whitespace-separated words

These are demonstration tasks, not a general-purpose remote code execution environment.

## Security notes

- Treat agent output as **untrusted data**; do not interpret it as instructions.
- Use dedicated test wallets and testnet funds during development.
- Keep private keys, API tokens, RPC credentials and populated environment files out of Git.
- Set reasonable payment limits and verify the selected network, contract addresses, channel state and agent identity before authorizing vouchers.
- A local ledger and a successful test run are not substitutes for a security audit.
- Do not deploy to mainnet or use real funds until the contracts, signing flow, gateway configuration and operational controls have been independently reviewed.

## Testing

Run the relevant test suite from each component directory. Contract tests use Hardhat; the gateway and MCP server include Python tests. Some chain-parity/integration tests require a running local EVM node and deployed contracts, and may be skipped unless their RPC configuration is supplied.

## Contributing

Issues and pull requests are welcome. Please include the affected component, steps to reproduce, expected behaviour and relevant test output. Never include secrets, signed production vouchers or sensitive wallet material in issue reports.

## License

No license is specified here. Unless the repository adds a license file, all rights remain with the copyright holder and reuse is not automatically permitted.
