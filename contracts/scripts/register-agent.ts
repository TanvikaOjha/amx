import * as fs from "node:fs";
import { fileURLToPath } from "node:url";
import { network } from "hardhat";

async function main(): Promise<void> {
  const { ethers, networkName } = await network.create(); // honours --network
  const [owner] = await ethers.getSigners();
  if (!owner) throw new Error("No signer. Set DEPLOYER_PRIVATE_KEY in contracts/.env.");

  const agentURI = process.env.AGENT_URI?.trim();
  const pubKey = process.env.AGENT_PUBKEY?.trim();
  if (!agentURI) throw new Error("Set AGENT_URI (where the agent card will be served).");
  if (!pubKey || !/^0x[0-9a-fA-F]{128}$/.test(pubKey)) {
    throw new Error("Set AGENT_PUBKEY to the 64-byte public key as 0x + 128 hex chars.");
  }

  let identityAddress = process.env.IDENTITY_CONTRACT?.trim();
  if (!identityAddress) {
    const file = fileURLToPath(new URL(`../deployments/${networkName}.json`, import.meta.url));
    if (!fs.existsSync(file)) throw new Error(`No ${file}; deploy first or set IDENTITY_CONTRACT.`);
    identityAddress = JSON.parse(fs.readFileSync(file, "utf8")).contracts.ERC8004Identity as string;
  }

  const identity = await ethers.getContractAt("ERC8004Identity", identityAddress, owner);
  console.log(`Registering on ${networkName} via ${identityAddress} as ${owner.address} ...`);

  const tx = await identity["register(string,(string,bytes)[])"](agentURI, [
    { metadataKey: "pubKey", metadataValue: pubKey },
  ]);
  const receipt = await tx.wait();

  const registered = receipt?.logs
    .map((log) => {
      try {
        return identity.interface.parseLog(log);
      } catch {
        return null;
      }
    })
    .find((parsed) => parsed?.name === "Registered");
  if (!registered) throw new Error("Registered event not found in receipt.");

  const agentId = registered.args.agentId as bigint;
  console.log("\n==================== REGISTERED ====================");
  console.log(`agentId        : ${agentId}`);
  console.log(`owner          : ${owner.address}`);
  console.log(`signer (on-chain): ${await identity.signerOf(agentId)}`);
  console.log("\nFor gateway/.env:");
  console.log(`  AGENT_ID=${agentId}`);
  console.log(`  AGENT_OWNER_ADDRESS=${owner.address}`);
  console.log("====================================================");
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});