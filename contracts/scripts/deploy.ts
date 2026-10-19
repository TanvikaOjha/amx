import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import hre from "hardhat";
import { verifyContract } from "@nomicfoundation/hardhat-verify/verify";

/**
 * Deploys ERC8004Identity and PaymentChannel, saves the addresses, and verifies the source.
 *
 *   npx hardhat run scripts/deploy.ts --network baseSepolia --build-profile production
 *   (or: npm run deploy:base-sepolia)
 *
 * Without --network it deploys to the in-process simulated chain, which is handy as a dry run.
 */

const EXPLORERS: Record<string, string> = {
  baseSepolia: "https://sepolia.basescan.org",
  sepolia: "https://sepolia.etherscan.io",
};

const GATEWAY_NETWORK_NAMES: Record<string, string> = {
  baseSepolia: "base-sepolia",
  sepolia: "sepolia",
  default: "local", // Hardhat 3 names the built-in simulated network "default"
  hardhatMainnet: "local",
  localhost: "local",
};

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Hardhat 3's verifyContract() takes ONE provider per call and throws on failure (it returns true both for
 * "verified now" and "already verified"). Running each provider on its own means a failure in one - a bad key,
 * an Etherscan plan restriction, indexing lag - never stops the other from running.
 */
async function verifyEverywhere(label: string, address: string, constructorArgs: unknown[]): Promise<void> {
  for (const provider of ["etherscan", "sourcify"] as const) {
    try {
      await verifyContract({ address, constructorArgs, provider }, hre);
      console.log(`  [${provider}] ${label}: verified`);
    } catch (err) {
      const first = (err instanceof Error ? err.message : String(err)).split("\n").find((l) => l.trim()) ?? "unknown error";
      console.warn(`  [${provider}] ${label}: FAILED -> ${first}`);
    }
  }
}

async function main(): Promise<void> {
  const connection = await hre.network.create(); // honours --network
  const { ethers, networkName } = connection;

  const signers = await ethers.getSigners();
  if (signers.length === 0) {
    throw new Error(`No deployer account for network "${networkName}". Set DEPLOYER_PRIVATE_KEY in contracts/.env (never in chat).`);
  }
  const deployer = signers[0];
  const chainId = (await ethers.provider.getNetwork()).chainId;
  // Local = the in-process simulated chain OR a local `hardhat node` (chain 31337): no confirmations to wait for, nothing to verify.
  const isLocal = connection.networkConfig.type === "edr-simulated" || chainId === 31337n;
  const minChallengePeriod = BigInt(process.env.MIN_CHALLENGE_PERIOD_SECONDS ?? "3600");

  console.log(`Network:   ${networkName} (chainId ${chainId})`);
  console.log(`Deployer:  ${deployer.address}`);
  const balance = await ethers.provider.getBalance(deployer.address);
  console.log(`Balance:   ${ethers.formatEther(balance)} ETH`);
  if (!isLocal && balance === 0n) {
    throw new Error("Deployer has zero balance. Fund it from a testnet faucet first.");
  }
  console.log(`Min challenge period: ${minChallengePeriod}s\n`);

  // 1. ERC8004Identity (no constructor args)
  console.log("Deploying ERC8004Identity ...");
  const identity = await ethers.deployContract("ERC8004Identity");
  await identity.waitForDeployment();
  const identityAddress = await identity.getAddress();
  console.log(`  -> ${identityAddress}`);

  // 2. PaymentChannel(identity, minChallengePeriod)
  console.log("Deploying PaymentChannel ...");
  const channelArgs: [string, bigint] = [identityAddress, minChallengePeriod];
  const channel = await ethers.deployContract("PaymentChannel", channelArgs);
  await channel.waitForDeployment();
  const channelAddress = await channel.getAddress();
  console.log(`  -> ${channelAddress}\n`);

  // 3. Persist addresses (public information, safe to commit)
  const record = {
    network: networkName,
    chainId: Number(chainId),
    deployer: deployer.address,
    deployedAt: new Date().toISOString(),
    contracts: {
      ERC8004Identity: identityAddress,
      PaymentChannel: channelAddress,
    },
    paymentChannel: {
      minChallengePeriodSeconds: Number(minChallengePeriod),
      eip712Domain: {
        name: "AMXPaymentChannel",
        version: "1",
        chainId: Number(chainId),
        verifyingContract: channelAddress,
      },
    },
  };
  const outDir = fileURLToPath(new URL("../deployments/", import.meta.url)); // ESM has no __dirname
  fs.mkdirSync(outDir, { recursive: true });
  const outFile = path.join(outDir, `${networkName}.json`);
  fs.writeFileSync(outFile, JSON.stringify(record, null, 2) + "\n");
  console.log(`Saved ${path.relative(process.cwd(), outFile)}\n`);

  // 4. Verify on the block explorer (live networks only)
  if (!isLocal) {
    console.log("Waiting for confirmations before verification ...");
    await identity.deploymentTransaction()?.wait(5);
    await channel.deploymentTransaction()?.wait(5);
    await sleep(15_000); // explorers index new contracts with a delay

    console.log("Verifying ...");
    await verifyEverywhere("ERC8004Identity", identityAddress, []);
    await verifyEverywhere("PaymentChannel", channelAddress, channelArgs);
  } else {
    console.log("Local network: skipping verification.");
  }

  // 5. Summary
  const explorer = EXPLORERS[networkName];
  console.log("\n==================== DEPLOYED ====================");
  console.log(`ERC8004Identity : ${identityAddress}`);
  console.log(`PaymentChannel  : ${channelAddress}`);
  if (explorer) {
    console.log(`\nExplorer (check for the green "Contract Source Code Verified" tick):`);
    console.log(`  ${explorer}/address/${identityAddress}#code`);
    console.log(`  ${explorer}/address/${channelAddress}#code`);
  }
  console.log("\nFor the gateway (gateway/.env), used in Phase 2:");
  console.log(`  CHAIN_NETWORK=${GATEWAY_NETWORK_NAMES[networkName] ?? networkName}`);
  console.log(`  CHAIN_ID=${chainId}`);
  console.log(`  IDENTITY_CONTRACT=${identityAddress}`);
  console.log(`  CHANNEL_CONTRACT=${channelAddress}`);
  console.log("==================================================");
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});