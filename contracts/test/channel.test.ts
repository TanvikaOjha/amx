import { expect } from "chai";
import { network } from "hardhat";
import { anyValue } from "@nomicfoundation/hardhat-ethers-chai-matchers/withArgs";

// Hardhat 3: one isolated simulated chain per test file; helpers hang off the connection, not a global.
const { ethers, networkHelpers } = await network.create();

// loadFixture snapshots state after the first call and restores it afterwards. It needs a NAMED function.
const loadFixture = <T>(fixture: () => Promise<T>): Promise<T> => networkHelpers.loadFixture(fixture);
import type { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/types";
import type { PaymentChannel } from "../types/ethers-contracts/index.js";

const { time } = networkHelpers;

const MIN_PERIOD = 60n;
const PERIOD = 3600n;
const DEPOSIT = ethers.parseEther("1");

// Must match PaymentChannel.sol and protocol section 3.
const VOUCHER_TYPES = {
  Voucher: [
    { name: "channelId", type: "bytes32" },
    { name: "agentId", type: "uint256" },
    { name: "cumulativeAmount", type: "uint256" },
    { name: "nonce", type: "uint64" },
    { name: "expiry", type: "uint64" },
    { name: "requestHash", type: "bytes32" },
  ],
};

interface VoucherFields {
  channelId: string;
  agentId: bigint;
  cumulativeAmount: bigint;
  nonce: bigint;
  expiry: bigint;
  requestHash: string;
}

describe("PaymentChannel", () => {
  async function domainFor(channel: PaymentChannel) {
    const { chainId } = await ethers.provider.getNetwork();
    return {
      name: "AMXPaymentChannel",
      version: "1",
      chainId,
      verifyingContract: await channel.getAddress(),
    };
  }

  async function sign(channel: PaymentChannel, signer: HardhatEthersSigner, v: VoucherFields) {
    return signer.signTypedData(await domainFor(channel), VOUCHER_TYPES, v);
  }

  async function deployFixture() {
    const [payer, agentOwner, newOwner, stranger, otherSigner] = await ethers.getSigners();
    const identity = await ethers.deployContract("ERC8004Identity");
    const channel = await ethers.deployContract("PaymentChannel", [await identity.getAddress(), MIN_PERIOD]);

    await identity.connect(agentOwner)["register(string)"]("https://agent.example/card.json");
    const agentId = 1n;

    return { identity, channel, payer, agentOwner, newOwner, stranger, otherSigner, agentId };
  }

  async function openFixture() {
    const base = await deployFixture();
    const tx = await base.channel.connect(base.payer).openChannel(base.agentId, PERIOD, { value: DEPOSIT });
    const receipt = await tx.wait();
    const parsed = receipt!.logs
      .map((l) => {
        try {
          return base.channel.interface.parseLog(l);
        } catch {
          return null;
        }
      })
      .find((l) => l?.name === "ChannelOpened");
    const channelId: string = parsed!.args.channelId;

    const voucher = (cumulativeAmount: bigint, nonce: bigint, extra: Partial<VoucherFields> = {}): VoucherFields => ({
      channelId,
      agentId: base.agentId,
      cumulativeAmount,
      nonce,
      expiry: BigInt(Math.floor(Date.now() / 1000) + 3600),
      requestHash: ethers.keccak256(ethers.toUtf8Bytes(`request-${nonce}`)),
      ...extra,
    });

    return { ...base, channelId, voucher };
  }

  // ---------------------------------------------------------------------
  describe("openChannel / topUp", () => {
    it("opens a channel and records its terms", async () => {
      const { channel, payer, agentId, channelId } = await loadFixture(openFixture);
      const ch = await channel.getChannel(channelId);
      expect(ch.payer).to.equal(payer.address);
      expect(ch.status).to.equal(1n); // Open
      expect(ch.agentId).to.equal(agentId);
      expect(ch.deposit).to.equal(DEPOSIT);
      expect(ch.challengePeriod).to.equal(PERIOD);
      expect(await ethers.provider.getBalance(await channel.getAddress())).to.equal(DEPOSIT);
    });

    it("emits ChannelOpened", async () => {
      const { channel, payer, agentId } = await loadFixture(deployFixture);
      await expect(channel.connect(payer).openChannel(agentId, PERIOD, { value: DEPOSIT }))
        .to.emit(channel, "ChannelOpened")
        .withArgs(anyValue, payer.address, agentId, DEPOSIT, PERIOD);
    });

    it("generates distinct ids for repeated opens", async () => {
      const { channel, payer, agentId } = await loadFixture(deployFixture);
      const a = await channel.connect(payer).openChannel.staticCall(agentId, PERIOD, { value: 1n });
      await channel.connect(payer).openChannel(agentId, PERIOD, { value: 1n });
      const b = await channel.connect(payer).openChannel.staticCall(agentId, PERIOD, { value: 1n });
      expect(a).to.not.equal(b);
    });

    it("rejects zero deposit, bad challenge period and unknown agents", async () => {
      const { channel, payer, agentId } = await loadFixture(deployFixture);
      await expect(channel.connect(payer).openChannel(agentId, PERIOD, { value: 0 })).to.be.revertedWithCustomError(
        channel,
        "ZeroDeposit",
      );
      await expect(channel.connect(payer).openChannel(agentId, MIN_PERIOD - 1n, { value: 1n }))
        .to.be.revertedWithCustomError(channel, "InvalidChallengePeriod")
        .withArgs(MIN_PERIOD - 1n);
      await expect(
        channel.connect(payer).openChannel(agentId, 31n * 24n * 3600n, { value: 1n }),
      ).to.be.revertedWithCustomError(channel, "InvalidChallengePeriod");
      await expect(channel.connect(payer).openChannel(42n, PERIOD, { value: 1n }))
        .to.be.revertedWithCustomError(channel, "UnknownAgent")
        .withArgs(42n);
    });

    it("only the payer can top up, and only while open", async () => {
      const { channel, payer, stranger, channelId } = await loadFixture(openFixture);
      await expect(channel.connect(stranger).topUp(channelId, { value: 1n })).to.be.revertedWithCustomError(
        channel,
        "NotPayer",
      );
      await expect(channel.connect(payer).topUp(channelId, { value: 5n }))
        .to.emit(channel, "ChannelToppedUp")
        .withArgs(channelId, 5n, DEPOSIT + 5n);
      await channel.connect(payer).startClose(channelId);
      await expect(channel.connect(payer).topUp(channelId, { value: 1n })).to.be.revertedWithCustomError(
        channel,
        "ChannelNotOpen",
      );
    });
  });

  // ---------------------------------------------------------------------
  describe("EIP-712 parity", () => {
    it("on-chain voucherDigest equals the ethers TypedDataEncoder hash", async () => {
      const { channel, voucher } = await loadFixture(openFixture);
      const v = voucher(250n, 3n);
      const onChain = await channel.voucherDigest(v);
      const offChain = ethers.TypedDataEncoder.hash(await domainFor(channel), VOUCHER_TYPES, v);
      expect(onChain).to.equal(offChain);
    });

    it("domainSeparator matches the ethers domain hash", async () => {
      const { channel } = await loadFixture(openFixture);
      expect(await channel.domainSeparator()).to.equal(ethers.TypedDataEncoder.hashDomain(await domainFor(channel)));
    });
  });

  // ---------------------------------------------------------------------
  describe("cooperative close", () => {
    it("settles immediately: payee gets the voucher amount, payer the remainder", async () => {
      const { channel, payer, agentOwner, channelId, voucher } = await loadFixture(openFixture);
      const v = voucher(ethers.parseEther("0.3"), 3n);
      const sig = await sign(channel, payer, v);

      await expect(channel.connect(agentOwner).close(v, sig))
        .to.emit(channel, "ChannelClosed")
        .withArgs(channelId, agentOwner.address, ethers.parseEther("0.3"), ethers.parseEther("0.7"));

      expect((await channel.getChannel(channelId)).status).to.equal(3n); // Closed
      expect(await channel.pending(agentOwner.address)).to.equal(ethers.parseEther("0.3"));
      expect(await channel.pending(payer.address)).to.equal(ethers.parseEther("0.7"));
    });

    it("withdraw pays out and zeroes the pending balance", async () => {
      const { channel, payer, agentOwner, voucher } = await loadFixture(openFixture);
      const v = voucher(ethers.parseEther("0.3"), 3n);
      await channel.connect(agentOwner).close(v, await sign(channel, payer, v));

      await expect(channel.connect(agentOwner).withdraw()).to.changeEtherBalances(
        ethers,
        [agentOwner, channel],
        [ethers.parseEther("0.3"), -ethers.parseEther("0.3")],
      );
      await expect(channel.connect(payer).withdraw()).to.changeEtherBalance(ethers, payer, ethers.parseEther("0.7"));
      expect(await channel.pending(agentOwner.address)).to.equal(0n);
      expect(await ethers.provider.getBalance(await channel.getAddress())).to.equal(0n);
      await expect(channel.connect(agentOwner).withdraw()).to.be.revertedWithCustomError(channel, "NothingToWithdraw");
    });

    it("caps the payout at the deposit when the voucher exceeds it", async () => {
      const { channel, payer, agentOwner, channelId, voucher } = await loadFixture(openFixture);
      const v = voucher(DEPOSIT * 10n, 9n);
      await expect(channel.connect(agentOwner).close(v, await sign(channel, payer, v)))
        .to.emit(channel, "ChannelClosed")
        .withArgs(channelId, agentOwner.address, DEPOSIT, 0n);
      expect(await channel.pending(payer.address)).to.equal(0n);
    });

    it("only the current agent owner may close", async () => {
      const { channel, payer, stranger, voucher } = await loadFixture(openFixture);
      const v = voucher(100n, 1n);
      const sig = await sign(channel, payer, v);
      await expect(channel.connect(stranger).close(v, sig)).to.be.revertedWithCustomError(channel, "NotPayee");
      await expect(channel.connect(payer).close(v, sig)).to.be.revertedWithCustomError(channel, "NotPayee");
    });

    it("pays whoever owns the agent NFT at settlement time", async () => {
      const { identity, channel, payer, agentOwner, newOwner, agentId, voucher } = await loadFixture(openFixture);
      await identity.connect(agentOwner).transferFrom(agentOwner.address, newOwner.address, agentId);
      const v = voucher(100n, 1n);
      const sig = await sign(channel, payer, v);
      await expect(channel.connect(agentOwner).close(v, sig)).to.be.revertedWithCustomError(channel, "NotPayee");
      await channel.connect(newOwner).close(v, sig);
      expect(await channel.pending(newOwner.address)).to.equal(100n);
    });

    it("rejects a voucher signed by anyone other than the payer", async () => {
      const { channel, agentOwner, otherSigner, voucher } = await loadFixture(openFixture);
      const v = voucher(100n, 1n);
      await expect(
        channel.connect(agentOwner).close(v, await sign(channel, otherSigner, v)),
      ).to.be.revertedWithCustomError(channel, "InvalidSignature");
    });

    it("rejects a tampered voucher (amount changed after signing)", async () => {
      const { channel, payer, agentOwner, voucher } = await loadFixture(openFixture);
      const v = voucher(100n, 1n);
      const sig = await sign(channel, payer, v);
      await expect(
        channel.connect(agentOwner).close({ ...v, cumulativeAmount: 999n }, sig),
      ).to.be.revertedWithCustomError(channel, "InvalidSignature");
    });

    it("rejects a voucher whose agentId differs from the channel's", async () => {
      const { channel, payer, agentOwner, voucher } = await loadFixture(openFixture);
      const v = voucher(100n, 1n, { agentId: 2n });
      await expect(
        channel.connect(agentOwner).close(v, await sign(channel, payer, v)),
      ).to.be.revertedWithCustomError(channel, "VoucherMismatch");
    });

    it("rejects unknown channels and double close", async () => {
      const { channel, payer, agentOwner, voucher } = await loadFixture(openFixture);
      const bogus = voucher(100n, 1n, { channelId: ethers.id("nope") });
      await expect(
        channel.connect(agentOwner).close(bogus, await sign(channel, payer, bogus)),
      ).to.be.revertedWithCustomError(channel, "UnknownChannel");

      const v = voucher(100n, 1n);
      const sig = await sign(channel, payer, v);
      await channel.connect(agentOwner).close(v, sig);
      await expect(channel.connect(agentOwner).close(v, sig)).to.be.revertedWithCustomError(
        channel,
        "ChannelNotActive",
      );
    });

    it("does not enforce voucher expiry on-chain (work was already delivered)", async () => {
      const { channel, payer, agentOwner, voucher } = await loadFixture(openFixture);
      const v = voucher(100n, 1n, { expiry: 1n }); // long past
      await expect(channel.connect(agentOwner).close(v, await sign(channel, payer, v))).to.not.revert(ethers);
    });
  });

  // ---------------------------------------------------------------------
  describe("unilateral close", () => {
    it("only the payer can start a close, once", async () => {
      const { channel, payer, agentOwner, channelId } = await loadFixture(openFixture);
      await expect(channel.connect(agentOwner).startClose(channelId)).to.be.revertedWithCustomError(
        channel,
        "NotPayer",
      );
      await expect(channel.connect(payer).startClose(channelId)).to.emit(channel, "CloseStarted");
      const ch = await channel.getChannel(channelId);
      expect(ch.status).to.equal(2n); // Closing
      expect(ch.closeDeadline).to.equal(BigInt(await time.latest()) + PERIOD);
      await expect(channel.connect(payer).startClose(channelId)).to.be.revertedWithCustomError(
        channel,
        "ChannelNotOpen",
      );
    });

    it("refunds everything to the payer if nobody submits a voucher", async () => {
      const { channel, payer, stranger, channelId } = await loadFixture(openFixture);
      await channel.connect(payer).startClose(channelId);
      await expect(channel.connect(stranger).finalizeClose(channelId)).to.be.revertedWithCustomError(
        channel,
        "ChallengeWindowOpen",
      );
      await time.increase(PERIOD);
      await channel.connect(stranger).finalizeClose(channelId);
      expect(await channel.pending(payer.address)).to.equal(DEPOSIT);
    });

    it("keeps the highest submitted voucher; lower ones never reduce it", async () => {
      const { channel, payer, agentOwner, stranger, channelId, voucher } = await loadFixture(openFixture);
      await channel.connect(payer).startClose(channelId);

      const low = voucher(100n, 1n);
      const high = voucher(400n, 4n);
      await channel.connect(stranger).submitVoucher(high, await sign(channel, payer, high)); // anyone may relay
      await channel.connect(agentOwner).submitVoucher(low, await sign(channel, payer, low));
      expect((await channel.getChannel(channelId)).claimed).to.equal(400n);

      await time.increase(PERIOD);
      await expect(channel.connect(stranger).finalizeClose(channelId))
        .to.emit(channel, "ChannelClosed")
        .withArgs(channelId, agentOwner.address, 400n, DEPOSIT - 400n);
      expect(await channel.pending(agentOwner.address)).to.equal(400n);
      expect(await channel.pending(payer.address)).to.equal(DEPOSIT - 400n);
    });

    it("rejects invalid vouchers and late submissions", async () => {
      const { channel, payer, agentOwner, otherSigner, channelId, voucher } = await loadFixture(openFixture);
      const v = voucher(100n, 1n);

      await expect(
        channel.connect(agentOwner).submitVoucher(v, await sign(channel, payer, v)),
      ).to.be.revertedWithCustomError(channel, "ChannelNotClosing");

      await channel.connect(payer).startClose(channelId);
      await expect(
        channel.connect(agentOwner).submitVoucher(v, await sign(channel, otherSigner, v)),
      ).to.be.revertedWithCustomError(channel, "InvalidSignature");

      await time.increase(PERIOD);
      await expect(
        channel.connect(agentOwner).submitVoucher(v, await sign(channel, payer, v)),
      ).to.be.revertedWithCustomError(channel, "ChallengeWindowOver");
    });

    it("lets the payee settle immediately inside the window, using the best known voucher", async () => {
      const { channel, payer, agentOwner, channelId, voucher } = await loadFixture(openFixture);
      await channel.connect(payer).startClose(channelId);

      const high = voucher(500n, 5n);
      await channel.connect(agentOwner).submitVoucher(high, await sign(channel, payer, high));

      // Payee closes with an OLDER voucher but is still owed the already-claimed 500.
      const old = voucher(200n, 2n);
      await expect(channel.connect(agentOwner).close(old, await sign(channel, payer, old)))
        .to.emit(channel, "ChannelClosed")
        .withArgs(channelId, agentOwner.address, 500n, DEPOSIT - 500n);
    });

    it("blocks cooperative close once the window has elapsed", async () => {
      const { channel, payer, agentOwner, channelId, voucher } = await loadFixture(openFixture);
      await channel.connect(payer).startClose(channelId);
      await time.increase(PERIOD);
      const v = voucher(100n, 1n);
      await expect(
        channel.connect(agentOwner).close(v, await sign(channel, payer, v)),
      ).to.be.revertedWithCustomError(channel, "ChallengeWindowOver");
    });

    it("cannot finalize an open or already-closed channel", async () => {
      const { channel, payer, agentOwner, channelId, voucher } = await loadFixture(openFixture);
      await expect(channel.finalizeClose(channelId)).to.be.revertedWithCustomError(channel, "ChannelNotClosing");
      const v = voucher(1n, 1n);
      await channel.connect(agentOwner).close(v, await sign(channel, payer, v));
      await expect(channel.finalizeClose(channelId)).to.be.revertedWithCustomError(channel, "ChannelNotClosing");
    });
  });

  // ---------------------------------------------------------------------
  describe("misc", () => {
    it("rejects stray ETH sent directly to the contract", async () => {
      const { channel, payer } = await loadFixture(deployFixture);
      await expect(payer.sendTransaction({ to: await channel.getAddress(), value: 1n })).to.revert(ethers);
    });

    it("constructor validates its arguments", async () => {
      await expect(ethers.deployContract("PaymentChannel", [ethers.ZeroAddress, 60n])).to.be.revertedWith("identity=0");
      await expect(
        ethers.deployContract("PaymentChannel", [ethers.Wallet.createRandom().address, 31n * 24n * 3600n]),
      ).to.be.revertedWith("min>max");
    });
  });
});