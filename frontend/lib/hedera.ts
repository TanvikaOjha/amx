/**
 * Hedera mirror-node access and client-side verification of AMX audit proofs.
 * Proof format and signing rule: gateway/app/hedera_logger.py
 *   gatewaySig = EIP-191 signature (by the agent's registered key) over sha256(canonical JSON of the other six fields).
 */
import { recoverMessageAddress, sha256, toBytes, type Address, type Hex } from 'viem';
import { canonicalJson } from './canonical';
import { HEDERA_NETWORK } from './constants';

const MIRROR: Record<string, string> = {
  testnet: 'https://testnet.mirrornode.hedera.com',
  previewnet: 'https://previewnet.mirrornode.hedera.com',
};

export const HASHSCAN = `https://hashscan.io/${HEDERA_NETWORK}`;
export const hashscanTopicUrl = (topic: string) => `${HASHSCAN}/topic/${topic}`;

export interface Proof {
  agentId: number;
  channelId: Hex;
  nonce: number;
  requestHash: Hex;
  responseHash: Hex;
  ts: number;
  gatewaySig: Hex;
}

export interface MirrorMessage {
  sequence_number: number;
  consensus_timestamp: string;
  payer_account_id?: string;
  message: string; // base64
}

const FIELDS = ['agentId', 'channelId', 'nonce', 'requestHash', 'responseHash', 'ts', 'gatewaySig'];

export async function fetchTopicMessages(topicId: string, opts: { limit?: number; sequence?: number } = {}): Promise<MirrorMessage[]> {
  const base = MIRROR[HEDERA_NETWORK];
  const url =
    opts.sequence !== undefined
      ? `${base}/api/v1/topics/${topicId}/messages/${opts.sequence}`
      : `${base}/api/v1/topics/${topicId}/messages?limit=${opts.limit ?? 25}&order=desc`;
  const res = await fetch(url, { cache: 'no-store' });
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`Mirror node returned HTTP ${res.status}.`);
  const json = await res.json();
  return opts.sequence !== undefined ? [json as MirrorMessage] : ((json.messages ?? []) as MirrorMessage[]);
}

export function decodeMessage(message: MirrorMessage): Uint8Array {
  const bin = atob(message.message);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

/** Consensus timestamp "1700000000.123456789" -> Date. */
export function consensusDate(ts: string): Date {
  return new Date(Number(ts.split('.')[0]) * 1000);
}

export function parseProof(bytes: Uint8Array): Proof | null {
  try {
    const p = JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
    if (Object.keys(p).sort().join() !== [...FIELDS].sort().join()) return null;
    const ints = ['agentId', 'nonce', 'ts'].every((k) => Number.isSafeInteger(p[k]));
    const hexes = ['channelId', 'requestHash', 'responseHash'].every((k) => typeof p[k] === 'string' && /^0x[0-9a-f]{64}$/.test(p[k] as string));
    if (!ints || !hexes || typeof p.gatewaySig !== 'string' || !/^0x[0-9a-fA-F]{130}$/.test(p.gatewaySig)) return null;
    return p as unknown as Proof;
  } catch {
    return null;
  }
}

/** Recover who signed the proof; null if the signature is malformed. */
export async function recoverProofSigner(proof: Proof): Promise<Address | null> {
  const { gatewaySig, ...unsigned } = proof;
  const digest = sha256(toBytes(canonicalJson(unsigned)));
  try {
    return await recoverMessageAddress({ message: { raw: digest }, signature: gatewaySig });
  } catch {
    return null;
  }
}

