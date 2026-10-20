import { getAddress, isAddress, parseAbi, type Address, type Chain } from 'viem';
import { baseSepolia, sepolia } from 'viem/chains';

// ---------------------------------------------------------------------------
// Chain
// ---------------------------------------------------------------------------

export type SupportedChainId = 84532 | 11155111;

export const SUPPORTED_CHAINS: Record<
  SupportedChainId,
  { chain: Chain; network: string; label: string; explorer: string }
> = {
  84532: { chain: baseSepolia as Chain, network: 'base-sepolia', label: 'Base Sepolia', explorer: 'https://sepolia.basescan.org' },
  // `network` must equal the gateway's CHAIN_NETWORK (deploy.ts prints "sepolia" for chain 11155111).
  11155111: { chain: sepolia as Chain, network: 'sepolia', label: 'Sepolia', explorer: 'https://sepolia.etherscan.io' },
};

export const CHAIN_ID: SupportedChainId = Number(process.env.NEXT_PUBLIC_CHAIN_ID) === 84532 ? 84532 : 11155111;
export const TARGET = SUPPORTED_CHAINS[CHAIN_ID];

export const RPC_URL: string | undefined =
  (CHAIN_ID === 84532 ? process.env.NEXT_PUBLIC_BASE_SEPOLIA_RPC_URL : process.env.NEXT_PUBLIC_SEPOLIA_RPC_URL) || undefined;

// ---------------------------------------------------------------------------
// Contracts (addresses come from contracts/scripts/deploy.ts)
// ---------------------------------------------------------------------------

const ZERO = '0x0000000000000000000000000000000000000000' as Address;

// contracts/deployments/sepolia.json
const REPO_DEPLOYMENT: Partial<Record<SupportedChainId, { identity: string; channel: string }>> = {
  11155111: {
    identity: '0xbE0C1abE04f11Ef80215910E8D3eCBAcaFc746cD',
    channel: '0x3D62427e47e8CCcA306ca340aab97C04bE101ef5',
  },
};

function resolveAddress(fromEnv: string | undefined, fallback: string | undefined): Address {
  const value = (fromEnv?.trim() || fallback || '').trim();
  // Lower-case first: a mistyped mixed-case checksum should not take the whole app down.
  return isAddress(value, { strict: false }) ? getAddress(value.toLowerCase()) : ZERO;
}

export const CONTRACT_ADDRESSES = {
  ERC8004_IDENTITY: resolveAddress(process.env.NEXT_PUBLIC_IDENTITY_ADDRESS, REPO_DEPLOYMENT[CHAIN_ID]?.identity),
  PAYMENT_CHANNEL: resolveAddress(process.env.NEXT_PUBLIC_PAYMENT_CHANNEL_ADDRESS, REPO_DEPLOYMENT[CHAIN_ID]?.channel),
};

export const CONTRACTS_CONFIGURED =
  CONTRACT_ADDRESSES.ERC8004_IDENTITY !== ZERO && CONTRACT_ADDRESSES.PAYMENT_CHANNEL !== ZERO;

export const DEPLOY_BLOCK: bigint | undefined = /^\d+$/.test(process.env.NEXT_PUBLIC_DEPLOY_BLOCK ?? '')
  ? BigInt(process.env.NEXT_PUBLIC_DEPLOY_BLOCK as string)
  : undefined;

// ---------------------------------------------------------------------------
// Gateway / Hedera / wallet
// ---------------------------------------------------------------------------

/** Same-origin proxy to the gateway (see next.config.mjs). */
export const GATEWAY_BASE = '/api/gateway';
export const GATEWAY_AGENT_ID = BigInt(/^\d+$/.test(process.env.NEXT_PUBLIC_GATEWAY_AGENT_ID ?? '') ? (process.env.NEXT_PUBLIC_GATEWAY_AGENT_ID as string) : '1');
export const TICKET_HEADER = 'X-402-Payment-Ticket';
export const CHANNEL_HINT_HEADER = 'X-AMX-Channel-Id';

export const MAX_PRICE_PER_CALL: bigint = /^\d+$/.test(process.env.NEXT_PUBLIC_MAX_PRICE_PER_CALL_WEI ?? '')
  ? BigInt(process.env.NEXT_PUBLIC_MAX_PRICE_PER_CALL_WEI as string)
  : 100_000_000_000_000n;

export const HEDERA_NETWORK: 'testnet' | 'previewnet' =
  process.env.NEXT_PUBLIC_HEDERA_NETWORK === 'previewnet' ? 'previewnet' : 'testnet';
export const HCS_TOPIC_ID: string | undefined = /^\d+\.\d+\.\d+$/.test(process.env.NEXT_PUBLIC_HCS_TOPIC_ID ?? '')
  ? (process.env.NEXT_PUBLIC_HCS_TOPIC_ID as string)
  : undefined;

export const WALLETCONNECT_PROJECT_ID = process.env.NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID || 'AMX_DEMO_PROJECT_ID';

export const CHANNEL_STATUS = { None: 0, Open: 1, Closing: 2, Closed: 3 } as const;
export const TASKS = ['echo', 'sha256', 'word_count'] as const;
export type TaskName = (typeof TASKS)[number];

// ---------------------------------------------------------------------------
// ABIs: match contracts/contracts/*.sol exactly
// ---------------------------------------------------------------------------

export const ERC8004_ABI = parseAbi([
  'function register(string agentURI_) returns (uint256 agentId)',
  'function agentURI(uint256 agentId) view returns (string)',
  'function pubKey(uint256 agentId) view returns (bytes)',
  'function signerOf(uint256 agentId) view returns (address)',
  'function totalAgents() view returns (uint256)',
  'function ownerOf(uint256 tokenId) view returns (address)',
  'function giveFeedback(uint256 agentId, uint8 score, string tag)',
  'function getSummary(uint256 agentId) view returns (uint256 clientCount, uint256 scoreSum)',
  'function getFeedback(uint256 agentId, address client) view returns (uint8 score, bool exists)',
  'event Registered(uint256 indexed agentId, string agentURI, address indexed owner)',
  'event FeedbackGiven(uint256 indexed agentId, address indexed client, uint8 score, string tag)',
  'error ERC721NonexistentToken(uint256 tokenId)',
  'error ScoreOutOfRange(uint8 score)',
  'error TagTooLong(uint256 length)',
  'error SelfFeedbackNotAllowed()',
]);

export const PAYMENT_CHANNEL_ABI = parseAbi([
  'struct Voucher { bytes32 channelId; uint256 agentId; uint256 cumulativeAmount; uint64 nonce; uint64 expiry; bytes32 requestHash; }',
  'struct Channel { address payer; uint8 status; uint64 challengePeriod; uint64 closeDeadline; uint256 agentId; uint256 deposit; uint256 claimed; }',
  'function openChannel(uint256 agentId, uint64 challengePeriod) payable returns (bytes32 channelId)',
  'function topUp(bytes32 channelId) payable',
  'function close(Voucher v, bytes signature)',
  'function startClose(bytes32 channelId)',
  'function submitVoucher(Voucher v, bytes signature)',
  'function finalizeClose(bytes32 channelId)',
  'function withdraw()',
  'function getChannel(bytes32 channelId) view returns (Channel)',
  'function pending(address account) view returns (uint256)',
  'function minChallengePeriod() view returns (uint64)',
  'function channelCount() view returns (uint256)',
  'event ChannelOpened(bytes32 indexed channelId, address indexed payer, uint256 indexed agentId, uint256 deposit, uint64 challengePeriod)',
  'event ChannelToppedUp(bytes32 indexed channelId, uint256 amount, uint256 newDeposit)',
  'event CloseStarted(bytes32 indexed channelId, uint64 deadline)',
  'event ChannelClosed(bytes32 indexed channelId, address indexed payee, uint256 payeeAmount, uint256 payerRefund)',
  'event Withdrawn(address indexed account, uint256 amount)',
  'error ZeroDeposit()',
  'error InvalidChallengePeriod(uint64 provided)',
  'error UnknownAgent(uint256 agentId)',
  'error UnknownChannel(bytes32 channelId)',
  'error NotPayer()',
  'error NotPayee()',
  'error ChannelNotOpen()',
  'error ChannelNotClosing()',
  'error ChannelNotActive()',
  'error ChallengeWindowOpen(uint64 deadline)',
  'error ChallengeWindowOver(uint64 deadline)',
  'error NothingToWithdraw()',
  'error TransferFailed()',
]);