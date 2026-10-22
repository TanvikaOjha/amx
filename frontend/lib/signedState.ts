import { CHAIN_ID } from './constants';
import { readJson, writeJson } from './storage';

/**
 * What THIS browser has signed per channel (like the MCP server's signer state file).
 * Spend accounting must not rely only on numbers the gateway reports in its 402 challenge.
 */
export interface SignedState {
  nonce: bigint;
  cumulative: bigint;
}

const key = (channelId: string) => `amx:signed:${CHAIN_ID}:${channelId}`;

export function loadSigned(channelId: string): SignedState | null {
  const v = readJson<{ nonce: string; cumulative: string } | null>(key(channelId), null);
  if (!v || !/^\d+$/.test(v.nonce) || !/^\d+$/.test(v.cumulative)) return null;
  return { nonce: BigInt(v.nonce), cumulative: BigInt(v.cumulative) };
}

export function saveSigned(channelId: string, state: SignedState | null): void {
  writeJson(key(channelId), state ? { nonce: state.nonce.toString(), cumulative: state.cumulative.toString() } : null);
}