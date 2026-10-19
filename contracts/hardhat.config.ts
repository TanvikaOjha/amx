import "dotenv/config"; // loads contracts/.env (git-ignored) into process.env; Hardhat 3 does not do this itself
import hardhatToolboxMochaEthers from "@nomicfoundation/hardhat-toolbox-mocha-ethers";
import { configVariable, defineConfig } from "hardhat/config";

/**
 * Hardhat 3 notes
 * - ESM only (package.json has "type": "module").
 * - Secrets use configVariable(): resolved lazily from the Hardhat keystore (`npx hardhat keystore set NAME`)
 *   or, as here, from environment variables / contracts/.env. A missing variable only errors when the
 *   network or verifier that needs it is actually used, so `hardhat test` works with an empty .env.
 * - The verify plugin defaults to the "production" build profile while `build`/`run` default to "default".
 *   If their compiler settings differed, deployed bytecode would not match verification input. Both profiles
 *   are therefore IDENTICAL on purpose, and the npm deploy scripts also pass --build-profile production.
 */
const solcSettings = {
  optimizer: { enabled: true, runs: 200 },
  evmVersion: "cancun",
} as const;

export default defineConfig({
  plugins: [hardhatToolboxMochaEthers],

  solidity: {
    profiles: {
      default: { version: "0.8.24", settings: solcSettings },
      production: { version: "0.8.24", settings: solcSettings },
    },
  },

  networks: {
    // In-process simulated chain used by `hardhat test` and `hardhat run` without --network.
    hardhatMainnet: { type: "edr-simulated", chainType: "l1" },
    localhost: { type: "http", chainType: "l1", chainId: 31337, url: "http://127.0.0.1:8545" },

    baseSepolia: {
      type: "http",
      chainType: "l1",
      chainId: 84532,
      url: process.env.BASE_SEPOLIA_RPC_URL || "https://sepolia.base.org",
      accounts: [configVariable("DEPLOYER_PRIVATE_KEY")],
    },
    sepolia: {
      type: "http",
      chainType: "l1",
      chainId: 11155111,
      url: configVariable("SEPOLIA_RPC_URL"),
      accounts: [configVariable("DEPLOYER_PRIVATE_KEY")],
    },
  },

  verify: {
    // Etherscan API V2: one key works for every supported chain, including Base Sepolia.
    etherscan: { apiKey: configVariable("ETHERSCAN_API_KEY") },
    // Keyless fallback verifier.
    sourcify: { enabled: true },
  },
});