'use client';

import { useQuery } from '@tanstack/react-query';
import type { Address } from 'viem';
import { publicClient } from '@/lib/chain';
import { CONTRACTS_CONFIGURED, CONTRACT_ADDRESSES, ERC8004_ABI } from '@/lib/constants';
import { consensusDate, decodeMessage, fetchTopicMessages, parseProof, recoverProofSigner, type Proof } from '@/lib/hedera';

export type Verification =
  | { state: 'valid'; signer: Address }
  | { state: 'wrong_signer'; signer: Address; expected: Address | null }
  | { state: 'bad_signature' }
  | { state: 'not_a_proof' };

export interface FeedRow {
  sequence: number;
  consensus: Date;
  payer?: string;
  sizeBytes: number;
  proof: Proof | null;
  verification: Verification;
}

/** Reads a topic from the mirror node and checks each proof's signature against ERC8004Identity.signerOf(agentId). */
export function useHcsFeed(topicId: string | undefined, opts: { limit?: number; sequence?: number; refetchMs?: number | false } = {}) {
  return useQuery({
    queryKey: ['amx', 'hcs', topicId, opts.limit, opts.sequence],
    enabled: !!topicId,
    refetchInterval: opts.refetchMs ?? false,
    queryFn: async (): Promise<FeedRow[]> => {
      const messages = await fetchTopicMessages(topicId as string, { limit: opts.limit, sequence: opts.sequence });
      const signers = new Map<number, Promise<Address | null>>();
      const expectedSigner = (agentId: number) => {
        if (!CONTRACTS_CONFIGURED) return Promise.resolve(null);
        if (!signers.has(agentId)) {
          signers.set(
            agentId,
            publicClient
              .readContract({ address: CONTRACT_ADDRESSES.ERC8004_IDENTITY, abi: ERC8004_ABI, functionName: 'signerOf', args: [BigInt(agentId)] })
              .catch(() => null),
          );
        }
        return signers.get(agentId) as Promise<Address | null>;
      };

      return Promise.all(
        messages.map(async (m): Promise<FeedRow> => {
          const bytes = decodeMessage(m);
          const proof = parseProof(bytes);
          const row = { sequence: m.sequence_number, consensus: consensusDate(m.consensus_timestamp), payer: m.payer_account_id, sizeBytes: bytes.length, proof };
          if (!proof) return { ...row, verification: { state: 'not_a_proof' } };
          const signer = await recoverProofSigner(proof);
          if (!signer) return { ...row, verification: { state: 'bad_signature' } };
          const expected = await expectedSigner(proof.agentId);
          const ok = expected !== null && expected !== '0x0000000000000000000000000000000000000000' && expected.toLowerCase() === signer.toLowerCase();
          return { ...row, verification: ok ? { state: 'valid', signer } : { state: 'wrong_signer', signer, expected } };
        }),
      );
    },
  });
}