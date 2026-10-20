import {
  BaseError,
  ContractFunctionRevertedError,
  UserRejectedRequestError,
  createPublicClient,
  getAbiItem,
  http,
  type Address,
  type Hex,
} from 'viem';
import { CHAIN_ID, CONTRACT_ADDRESSES, DEPLOY_BLOCK, ERC8004_ABI, PAYMENT_CHANNEL_ABI, RPC_URL, TARGET } from './constants';
import { readJson, writeJson } from './storage';

/** Read-only client for queries that are awkward through wagmi hooks (logs, fan-out reads). */
export const publicClient = createPublicClient({
  chain: TARGET.chain,
  transport: http(RPC_URL),
  batch: { multicall: true },
});

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

const ERROR_TEXT: Record<string, string> = {
  ZeroDeposit: 'The deposit must be greater than zero.',
  InvalidChallengePeriod: 'The challenge period is outside the contract’s allowed range.',
  UnknownAgent: 'That agent id is not registered.',
  UnknownChannel: 'That channel does not exist.',
  NotPayer: 'Only the account that opened the channel can do this.',
  NotPayee: 'Only the agent owner can do this.',
  ChannelNotOpen: 'The channel is not open.',
  ChannelNotClosing: 'The channel is not in its closing window.',
  ChannelNotActive: 'The channel is already closed.',
  ChallengeWindowOpen: 'The challenge window is still open. Wait until it ends.',
  ChallengeWindowOver: 'The challenge window has ended.',
  NothingToWithdraw: 'There is nothing to withdraw.',
  TransferFailed: 'The ETH transfer failed.',
  ScoreOutOfRange: 'The score must be between 0 and 100.',
  TagTooLong: 'The tag is longer than 64 bytes.',
  SelfFeedbackNotAllowed: 'The owner (or an approved operator) cannot rate their own agent.',
  ERC721NonexistentToken: 'That agent does not exist.',
};

export function isRevert(err: unknown): boolean {
  return err instanceof BaseError && err.walk((e) => e instanceof ContractFunctionRevertedError) !== null;
}

/** Turn viem / wagmi errors into one readable sentence. */
export function errorMessage(err: unknown): string {
  if (err instanceof BaseError) {
    if (err.walk((e) => e instanceof UserRejectedRequestError)) return 'You rejected the request in your wallet.';
    const reverted = err.walk((e) => e instanceof ContractFunctionRevertedError);
    if (reverted instanceof ContractFunctionRevertedError) {
      const name = reverted.data?.errorName;
      if (name && ERROR_TEXT[name]) return ERROR_TEXT[name];
      return reverted.reason ?? name ?? reverted.shortMessage;
    }
    return err.shortMessage;
  }
  return err instanceof Error ? err.message : 'Something went wrong.';
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export interface AgentOnChain {
  id: bigint;
  owner: Address;
  uri: string;
  pubKey: Hex;
  signer: Address;
  clientCount: bigint;
  scoreSum: bigint;
}

const identity = { address: CONTRACT_ADDRESSES.ERC8004_IDENTITY, abi: ERC8004_ABI } as const;
const channel = { address: CONTRACT_ADDRESSES.PAYMENT_CHANNEL, abi: PAYMENT_CHANNEL_ABI } as const;

export async function readTotalAgents(): Promise<bigint> {
  return publicClient.readContract({ ...identity, functionName: 'totalAgents' });
}

/** null = the agent does not exist; any other failure (RPC down, wrong address) throws. */
export async function readAgent(id: bigint): Promise<AgentOnChain | null> {
  let owner: Address;
  try {
    owner = await publicClient.readContract({ ...identity, functionName: 'ownerOf', args: [id] });
  } catch (err) {
    if (isRevert(err)) return null;
    throw err;
  }
  const [uri, pubKey, signer, summary] = await Promise.all([
    publicClient.readContract({ ...identity, functionName: 'agentURI', args: [id] }),
    publicClient.readContract({ ...identity, functionName: 'pubKey', args: [id] }),
    publicClient.readContract({ ...identity, functionName: 'signerOf', args: [id] }),
    publicClient.readContract({ ...identity, functionName: 'getSummary', args: [id] }),
  ]);
  return { id, owner, uri, pubKey, signer, clientCount: summary[0], scoreSum: summary[1] };
}

/** Scores are 0–100 per rater (one latest score per rater); the average is scoreSum / clientCount. */
export function reputation(a: Pick<AgentOnChain, 'clientCount' | 'scoreSum'>): { average: number | null; count: number } {
  const count = Number(a.clientCount);
  return { count, average: count === 0 ? null : Number(a.scoreSum) / count };
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

export interface ChannelView {
  id: Hex;
  payer: Address;
  status: number; // 1 open, 2 closing, 3 closed
  challengePeriod: bigint;
  closeDeadline: bigint;
  agentId: bigint;
  deposit: bigint;
  claimed: bigint;
}

export async function readChannel(id: Hex): Promise<ChannelView | null> {
  const ch = await publicClient.readContract({ ...channel, functionName: 'getChannel', args: [id] });
  if (ch.status === 0) return null;
  return {
    id,
    payer: ch.payer,
    status: ch.status,
    challengePeriod: BigInt(ch.challengePeriod),
    closeDeadline: BigInt(ch.closeDeadline),
    agentId: ch.agentId,
    deposit: ch.deposit,
    claimed: ch.claimed,
  };
}

const knownKey = (payer: Address) => `amx:channels:${CHAIN_ID}:${payer.toLowerCase()}`;

export function loadKnownChannels(payer: Address): Hex[] {
  return readJson<Hex[]>(knownKey(payer), []);
}

export function rememberChannel(payer: Address, id: Hex): void {
  const list = loadKnownChannels(payer);
  if (!list.includes(id)) writeJson(knownKey(payer), [...list, id]);
}

const OPENED = getAbiItem({ abi: PAYMENT_CHANNEL_ABI, name: 'ChannelOpened' });

/** All channels this payer ever opened, from ChannelOpened logs (adaptive chunking for RPC range limits). */
export async function fetchOpenedChannelIds(payer: Address): Promise<Hex[]> {
  const latest = await publicClient.getBlockNumber();
  const start = DEPLOY_BLOCK ?? (latest > 200_000n ? latest - 200_000n : 0n);
  const ids: Hex[] = [];
  let from = start;
  let step = 50_000n;
  while (from <= latest) {
    const to = from + step - 1n > latest ? latest : from + step - 1n;
    try {
      const logs = await publicClient.getLogs({
        address: CONTRACT_ADDRESSES.PAYMENT_CHANNEL,
        event: OPENED,
        args: { payer },
        fromBlock: from,
        toBlock: to,
      });
      for (const log of logs) if (log.args.channelId) ids.push(log.args.channelId);
      from = to + 1n;
    } catch (err) {
      if (step <= 2_000n) throw err;
      step = step / 2n; // RPC rejected the range: retry smaller
    }
  }
  return ids;
}