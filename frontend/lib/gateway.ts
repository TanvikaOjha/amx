
import {
  CHANNEL_HINT_HEADER,
  CHAIN_ID,
  CONTRACT_ADDRESSES,
  GATEWAY_AGENT_ID,
  GATEWAY_BASE,
  MAX_PRICE_PER_CALL,
  TARGET,
  TICKET_HEADER,
} from './constants';
import type { SignedState } from './signedState';

export class AmxError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

// Low-level HTTP

export interface Reply {
  status: number;
  json: unknown;
}

async function request(path: string, init: RequestInit, timeoutMs = 45_000): Promise<Reply> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${GATEWAY_BASE}${path}`, { ...init, signal: controller.signal, redirect: 'manual', cache: 'no-store' });
    let json: unknown = null;
    try {
      json = await res.json();
    } catch {
      /* non-JSON body */
    }
    return { status: res.status, json };
  } finally {
    clearTimeout(timer);
  }
}

export async function postExecute(bodyText: string, headers: Record<string, string>): Promise<Reply> {
  return request('/v1/execute', { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: bodyText });
}

// The 402 challenge

export interface Accept {
  price: bigint;
  minNonce: bigint;
  minCumulative: bigint;
}

const UINT = /^(0|[1-9][0-9]{0,77})$/;
const UINT256_MAX = 2n ** 256n - 1n;
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

function parseUint(value: unknown, field: string): bigint {
  if (typeof value !== 'string' || !UINT.test(value) || BigInt(value) > UINT256_MAX) {
    throw new AmxError('malformed_challenge', `The challenge field “${field}” is not a valid amount.`);
  }
  return BigInt(value);
}

export function selectAccept(challenge: unknown): Accept {
  if (!isRecord(challenge) || challenge.error !== 'payment_required' || !Array.isArray(challenge.accepts) || challenge.accepts.length === 0) {
    throw new AmxError('malformed_challenge', 'The gateway returned a 402 that is not an AMX payment challenge.');
  }
  const problems: string[] = [];
  for (const option of challenge.accepts) {
    if (!isRecord(option)) {
      problems.push('malformed option');
      continue;
    }
    const issues: string[] = [];
    if (option.scheme !== 'amx-voucher') issues.push('scheme');
    if (option.asset !== 'native') issues.push('asset');
    if (option.network !== TARGET.network) issues.push(`network (expected ${TARGET.network})`);
    if (typeof option.agentId !== 'number' || BigInt(option.agentId) !== GATEWAY_AGENT_ID) issues.push('agent id');
    if (typeof option.channelContract !== 'string' || option.channelContract.toLowerCase() !== CONTRACT_ADDRESSES.PAYMENT_CHANNEL.toLowerCase()) {
      issues.push('channel contract');
    }
    if (issues.length) {
      problems.push(issues.join(', '));
      continue;
    }
    if (typeof option.minNonce !== 'number' || !Number.isSafeInteger(option.minNonce) || option.minNonce < 1) {
      throw new AmxError('malformed_challenge', 'The challenge nonce is out of range.');
    }
    return {
      price: parseUint(option.pricePerCall, 'pricePerCall'),
      minNonce: BigInt(option.minNonce),
      minCumulative: parseUint(option.minCumulativeAmount, 'minCumulativeAmount'),
    };
  }
  throw new AmxError('challenge_mismatch', `No acceptable payment option. The gateway’s offer differs from this app’s configuration: ${problems.join('; ')}.`);
}

export interface Plan {
  nonce: bigint;
  cumulative: bigint;
  price: bigint;
  /** What the gateway says the channel total was before this call. */
  gatewayPrevious: bigint;
  /** The gateway's ledger is ahead of what this browser has signed: lost local state, another device, or a lying gateway. */
  gatewayAhead: boolean;
}

export function planVoucher(accept: Accept, local: SignedState | null): Plan {
  if (accept.price > MAX_PRICE_PER_CALL) {
    throw new AmxError('price_exceeds_cap', `The gateway’s price (${accept.price} wei) is above this app’s per-call cap (${MAX_PRICE_PER_CALL} wei). Nothing was signed.`);
  }
  const lastNonce = local?.nonce ?? 0n;
  const lastCumulative = local?.cumulative ?? 0n;
  const nonce = accept.minNonce > lastNonce + 1n ? accept.minNonce : lastNonce + 1n;
  const floor = lastCumulative + accept.price;
  const cumulative = accept.minCumulative > floor ? accept.minCumulative : floor;
  return {
    nonce,
    cumulative,
    price: accept.price,
    gatewayPrevious: accept.minCumulative - accept.price,
    gatewayAhead: accept.minNonce > lastNonce + 1n || accept.minCumulative > floor,
  };
}

// Result + error handling

export interface GatewayResult {
  job_id: string;
  task: string;
  output: string;
  request_hash: string;
  response_hash: string;
  channel_id: string;
  nonce: number;
  cumulative_amount: string;
  charged_amount: string;
  idempotent_replay: boolean;
  hcs_status: 'confirmed' | 'pending' | 'failed' | 'disabled' | 'unknown';
  hcs_topic_id: string | null;
  hcs_sequence_number: number | null;
}

const TOPIC = /^\d{1,10}\.\d{1,10}\.\d{1,12}$/;
const str = (v: unknown): v is string => typeof v === 'string';

export function parseResult(json: unknown): GatewayResult | null {
  if (!isRecord(json)) return null;
  const j = json;
  if (!str(j.job_id) || !str(j.task) || !str(j.output) || !str(j.request_hash) || !str(j.response_hash) || !str(j.channel_id)) return null;
  if (typeof j.nonce !== 'number' || !str(j.cumulative_amount) || !str(j.charged_amount)) return null;
  const hcs = ['confirmed', 'pending', 'failed', 'disabled'].includes(j.hcs_status as string) ? (j.hcs_status as GatewayResult['hcs_status']) : 'unknown';
  return {
    job_id: j.job_id,
    task: j.task,
    output: j.output,
    request_hash: j.request_hash,
    response_hash: j.response_hash,
    channel_id: j.channel_id,
    nonce: j.nonce,
    cumulative_amount: j.cumulative_amount,
    charged_amount: j.charged_amount,
    idempotent_replay: j.idempotent_replay === true,
    hcs_status: hcs,
    hcs_topic_id: str(j.hcs_topic_id) && TOPIC.test(j.hcs_topic_id) ? j.hcs_topic_id : null,
    hcs_sequence_number: typeof j.hcs_sequence_number === 'number' && j.hcs_sequence_number > 0 ? j.hcs_sequence_number : null,
  };
}

const REASON_HINTS: Record<string, string> = {
  malformed_ticket: 'The gateway could not parse the payment ticket.',
  wrong_agent: 'The ticket names a different agent than this gateway serves.',
  request_hash_mismatch: 'The ticket does not match the request body.',
  expired: 'The voucher expired before the gateway processed it.',
  invalid_signature: 'The voucher signature was not accepted.',
  unknown_channel: 'The channel does not exist on-chain.',
  channel_not_open: 'The channel is closing or closed.',
  channel_agent_mismatch: 'The channel belongs to a different agent.',
  signer_not_payer: 'The voucher was not signed by the account that opened the channel. Connect that account.',
  challenge_period_too_short: 'The channel’s challenge period is shorter than the gateway accepts.',
  exceeds_deposit: 'The voucher would exceed the channel deposit. Top the channel up.',
  nonce_not_increasing: 'The gateway already has an equal or newer voucher for this channel. This browser’s record may be out of sync.',
  insufficient_cumulative_amount: 'The voucher did not raise the total by the price.',
  invalid_request: 'The gateway rejected the request body.',
  body_too_large: 'The request body is too large.',
  rate_limited: 'The gateway is rate limiting you. Try again shortly.',
  execution_failed: 'The task failed on the gateway.',
  agent_not_registered: 'The gateway’s agent is not registered on-chain.',
  agent_registry_mismatch: 'The gateway’s on-chain agent record does not match its configuration.',
  chain_unavailable: 'The gateway could not read chain state.',
};

const CODE = /^[a-z][a-z0-9_]{0,63}$/;
export const describeReason = (code: string | null) => REASON_HINTS[code ?? ''] ?? 'The gateway reported an error (details withheld: untrusted text).';

export type PaidOutcome =
  | { kind: 'ok'; result: GatewayResult }
  | { kind: 'malformed' } // 200 we cannot read: we WERE charged
  | { kind: 'rejected'; code: string; status: number } // definitively not charged
  | { kind: 'in_progress' }
  | { kind: 'unknown'; status: number | null }; // ticket sent, fate unknown: re-send the same ticket

/** The single paid attempt. Same classification rules as mcp_server/server.py. */
export async function submitPaid(bodyText: string, ticket: string): Promise<PaidOutcome> {
  let reply: Reply;
  try {
    reply = await postExecute(bodyText, { [TICKET_HEADER]: ticket });
  } catch {
    return { kind: 'unknown', status: null }; // browsers cannot tell "never sent" from "response lost"
  }
  const json = isRecord(reply.json) ? reply.json : null;
  const gatewayJson = json !== null && typeof json.error === 'string';
  const rawCode = json ? (json.reason ?? json.error) : null;
  const code = typeof rawCode === 'string' && CODE.test(rawCode) ? rawCode : null;

  if (reply.status === 200) {
    const result = parseResult(reply.json);
    return result ? { kind: 'ok', result } : { kind: 'malformed' };
  }
  if (reply.status > 200 && reply.status < 300) return { kind: 'unknown', status: reply.status };
  if (reply.status === 409 && gatewayJson && json?.error === 'request_in_progress') return { kind: 'in_progress' };
  if (reply.status >= 500 && !(gatewayJson && (reply.status === 500 || reply.status === 503))) return { kind: 'unknown', status: reply.status };
  return { kind: 'rejected', code: code ?? `http_${reply.status}`, status: reply.status };
}

// ---------------------------------------------------------------------------
// Read-only endpoints
// ---------------------------------------------------------------------------

export interface Health {
  status: string;
  agentId: number;
  network: string;
  registry: string;
  hcs: string;
}

export async function fetchHealth(): Promise<Health> {
  const r = await request('/health', { method: 'GET' }, 10_000);
  const j = r.json;
  if (r.status !== 200 || !isRecord(j) || typeof j.status !== 'string') throw new AmxError('gateway_unreachable', 'The gateway did not return a health report.');
  return { status: j.status, agentId: Number(j.agentId), network: String(j.network), registry: String(j.registry), hcs: String(j.hcs ?? 'disabled') };
}

export interface LedgerState {
  lastNonce: number;
  lastCumulativeAmount: string;
}

export async function fetchLedger(channelId: string): Promise<LedgerState | null> {
  try {
    const r = await request(`/v1/channels/${channelId}`, { method: 'GET' }, 10_000);
    if (r.status !== 200 || !isRecord(r.json) || typeof r.json.lastNonce !== 'number' || !str(r.json.lastCumulativeAmount)) return null;
    return { lastNonce: r.json.lastNonce, lastCumulativeAmount: r.json.lastCumulativeAmount };
  } catch {
    return null;
  }
}

export async function fetchGatewayAgentCard(): Promise<unknown> {
  const r = await request('/.well-known/agent-card.json', { method: 'GET' }, 10_000);
  if (r.status !== 200) throw new AmxError('card_unavailable', 'The gateway did not serve an agent card.');
  return r.json;
}

export { CHAIN_ID };