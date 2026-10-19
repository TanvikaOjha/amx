import { expect } from "chai";
import { network } from "hardhat";
import { anyValue } from "@nomicfoundation/hardhat-ethers-chai-matchers/withArgs";

// Hardhat 3: one isolated simulated chain per test file; helpers hang off the connection, not a global.
const { ethers, networkHelpers } = await network.create();

// loadFixture snapshots state after the first call and restores it afterwards. It needs a NAMED function.
const loadFixture = <T>(fixture: () => Promise<T>): Promise<T> => networkHelpers.loadFixture(fixture);

const AGENT_URI = "https://agent.example.com/.well-known/agent-card.json";

/** Uncompressed secp256k1 public key as 64 raw bytes (x||y), without the 0x04 prefix. */
function rawPubKey(wallet: { signingKey: { publicKey: string } }): string {
  return "0x" + wallet.signingKey.publicKey.slice(4);
}

describe("ERC8004Identity", () => {
  async function deployFixture() {
    const [deployer, owner, operator, stranger, client1, client2] = await ethers.getSigners();
    const identity = await ethers.deployContract("ERC8004Identity");
    return { identity, deployer, owner, operator, stranger, client1, client2 };
  }

  async function registeredFixture() {
    const base = await deployFixture();
    const agentKey = ethers.Wallet.createRandom();
    await base.identity
      .connect(base.owner)
      ["register(string,(string,bytes)[])"](AGENT_URI, [{ metadataKey: "pubKey", metadataValue: rawPubKey(agentKey) }]);
    return { ...base, agentKey, agentId: 1n };
  }

  describe("registration", () => {
    it("starts agent ids at 1 and increments", async () => {
      const { identity, owner } = await loadFixture(deployFixture);
      await identity.connect(owner)["register(string)"](AGENT_URI);
      await identity.connect(owner)["register()"]();
      expect(await identity.ownerOf(1n)).to.equal(owner.address);
      expect(await identity.ownerOf(2n)).to.equal(owner.address);
      expect(await identity.totalAgents()).to.equal(2n);
    });

    it("emits Registered and stores the agentURI", async () => {
      const { identity, owner } = await loadFixture(deployFixture);
      await expect(identity.connect(owner)["register(string)"](AGENT_URI))
        .to.emit(identity, "Registered")
        .withArgs(1n, AGENT_URI, owner.address);
      expect(await identity.agentURI(1n)).to.equal(AGENT_URI);
      expect(await identity.tokenURI(1n)).to.equal(AGENT_URI);
    });

    it("register() with no URI yields an empty agentURI", async () => {
      const { identity, owner } = await loadFixture(deployFixture);
      await identity.connect(owner)["register()"]();
      expect(await identity.agentURI(1n)).to.equal("");
    });

    it("stores metadata supplied at registration and emits MetadataSet", async () => {
      const { identity, owner } = await loadFixture(deployFixture);
      const value = ethers.toUtf8Bytes("hello");
      await expect(
        identity.connect(owner)["register(string,(string,bytes)[])"](AGENT_URI, [
          { metadataKey: "greeting", metadataValue: value },
        ]),
      )
        .to.emit(identity, "MetadataSet")
        .withArgs(1n, anyValue, "greeting", ethers.hexlify(value));
      expect(await identity.getMetadata(1n, "greeting")).to.equal(ethers.hexlify(value));
    });

    it("supports ERC-721 and ERC-4906 interfaces", async () => {
      const { identity } = await loadFixture(deployFixture);
      expect(await identity.supportsInterface("0x80ac58cd")).to.equal(true); // ERC-721
      expect(await identity.supportsInterface("0x49064906")).to.equal(true); // ERC-4906
    });
  });

  describe("pubKey", () => {
    it("exposes pubKey and the derived signer address", async () => {
      const { identity, agentKey, agentId } = await loadFixture(registeredFixture);
      expect(await identity.pubKey(agentId)).to.equal(rawPubKey(agentKey));
      expect(await identity.signerOf(agentId)).to.equal(agentKey.address);
    });

    it("rejects a pubKey that is not 64 bytes", async () => {
      const { identity, owner } = await loadFixture(deployFixture);
      await expect(
        identity.connect(owner)["register(string,(string,bytes)[])"](AGENT_URI, [
          { metadataKey: "pubKey", metadataValue: "0x1234" },
        ]),
      ).to.be.revertedWithCustomError(identity, "InvalidPubKey");

      await identity.connect(owner)["register(string)"](AGENT_URI);
      await expect(identity.connect(owner).setMetadata(1n, "pubKey", "0x1234")).to.be.revertedWithCustomError(
        identity,
        "InvalidPubKey",
      );
    });

    it("allows clearing the pubKey with empty bytes; signerOf then returns zero address", async () => {
      const { identity, owner, agentId } = await loadFixture(registeredFixture);
      await identity.connect(owner).setMetadata(agentId, "pubKey", "0x");
      expect(await identity.pubKey(agentId)).to.equal("0x");
      expect(await identity.signerOf(agentId)).to.equal(ethers.ZeroAddress);
    });

    it("clears the pubKey on transfer but keeps other metadata", async () => {
      const { identity, owner, stranger, agentId } = await loadFixture(registeredFixture);
      await identity.connect(owner).setMetadata(agentId, "note", ethers.toUtf8Bytes("keep me"));

      await expect(identity.connect(owner).transferFrom(owner.address, stranger.address, agentId))
        .to.emit(identity, "MetadataSet")
        .withArgs(agentId, anyValue, "pubKey", "0x");

      expect(await identity.ownerOf(agentId)).to.equal(stranger.address);
      expect(await identity.pubKey(agentId)).to.equal("0x");
      expect(await identity.signerOf(agentId)).to.equal(ethers.ZeroAddress);
      expect(await identity.getMetadata(agentId, "note")).to.equal(ethers.hexlify(ethers.toUtf8Bytes("keep me")));
    });
  });

  describe("updates and authorization", () => {
    it("owner can setAgentURI and setMetadata; events are emitted", async () => {
      const { identity, owner, agentId } = await loadFixture(registeredFixture);
      await expect(identity.connect(owner).setAgentURI(agentId, "https://new.example/card.json"))
        .to.emit(identity, "URIUpdated")
        .withArgs(agentId, "https://new.example/card.json", owner.address);
      expect(await identity.agentURI(agentId)).to.equal("https://new.example/card.json");

      await expect(identity.connect(owner).setMetadata(agentId, "k", "0x01"))
        .to.emit(identity, "MetadataSet")
        .withArgs(agentId, anyValue, "k", "0x01");
    });

    it("an approved operator can update; a stranger cannot", async () => {
      const { identity, owner, operator, stranger, agentId } = await loadFixture(registeredFixture);
      await identity.connect(owner).approve(operator.address, agentId);
      await expect(identity.connect(operator).setAgentURI(agentId, "https://op.example/card.json")).to.not.revert(ethers);

      await expect(identity.connect(stranger).setAgentURI(agentId, "x")).to.be.revertedWithCustomError(
        identity,
        "ERC721InsufficientApproval",
      );
      await expect(identity.connect(stranger).setMetadata(agentId, "k", "0x01")).to.be.revertedWithCustomError(
        identity,
        "ERC721InsufficientApproval",
      );
    });

    it("rejects an empty metadata key", async () => {
      const { identity, owner, agentId } = await loadFixture(registeredFixture);
      await expect(identity.connect(owner).setMetadata(agentId, "", "0x01")).to.be.revertedWithCustomError(
        identity,
        "EmptyMetadataKey",
      );
    });

    it("reverts on unknown agent ids", async () => {
      const { identity, owner } = await loadFixture(deployFixture);
      await expect(identity.connect(owner).setAgentURI(99n, "x")).to.be.revertedWithCustomError(
        identity,
        "ERC721NonexistentToken",
      );
      await expect(identity.pubKey(99n)).to.be.revertedWithCustomError(identity, "ERC721NonexistentToken");
      await expect(identity.getMetadata(99n, "k")).to.be.revertedWithCustomError(identity, "ERC721NonexistentToken");
    });
  });

  describe("reputation", () => {
    it("records feedback and aggregates it", async () => {
      const { identity, client1, client2, agentId } = await loadFixture(registeredFixture);
      await expect(identity.connect(client1).giveFeedback(agentId, 80, "fast"))
        .to.emit(identity, "FeedbackGiven")
        .withArgs(agentId, client1.address, 80, "fast");
      await identity.connect(client2).giveFeedback(agentId, 60, "ok");

      const [count, sum] = await identity.getSummary(agentId);
      expect(count).to.equal(2n);
      expect(sum).to.equal(140n);
    });

    it("replaces a client's previous score instead of double counting", async () => {
      const { identity, client1, agentId } = await loadFixture(registeredFixture);
      await identity.connect(client1).giveFeedback(agentId, 90, "a");
      await identity.connect(client1).giveFeedback(agentId, 20, "b");
      const [count, sum] = await identity.getSummary(agentId);
      expect(count).to.equal(1n);
      expect(sum).to.equal(20n);
      const [score, exists] = await identity.getFeedback(agentId, client1.address);
      expect(score).to.equal(20n);
      expect(exists).to.equal(true);
    });

    it("rejects scores above 100 and overlong tags", async () => {
      const { identity, client1, agentId } = await loadFixture(registeredFixture);
      await expect(identity.connect(client1).giveFeedback(agentId, 101, "x"))
        .to.be.revertedWithCustomError(identity, "ScoreOutOfRange")
        .withArgs(101);
      await expect(identity.connect(client1).giveFeedback(agentId, 50, "x".repeat(65))).to.be.revertedWithCustomError(
        identity,
        "TagTooLong",
      );
    });

    it("blocks the owner and approved operators from rating their own agent", async () => {
      const { identity, owner, operator, agentId } = await loadFixture(registeredFixture);
      await expect(identity.connect(owner).giveFeedback(agentId, 100, "me")).to.be.revertedWithCustomError(
        identity,
        "SelfFeedbackNotAllowed",
      );
      await identity.connect(owner).approve(operator.address, agentId);
      await expect(identity.connect(operator).giveFeedback(agentId, 100, "me")).to.be.revertedWithCustomError(
        identity,
        "SelfFeedbackNotAllowed",
      );
    });

    it("reverts for unknown agents", async () => {
      const { identity, client1 } = await loadFixture(deployFixture);
      await expect(identity.connect(client1).giveFeedback(5n, 50, "x")).to.be.revertedWithCustomError(
        identity,
        "ERC721NonexistentToken",
      );
    });
  });
});