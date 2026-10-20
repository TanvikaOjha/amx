'use client';

import { useQuery } from '@tanstack/react-query';
import { useWriteContract } from 'wagmi';
import type { Address } from 'viem';
import { fetchAgentCard } from '@/lib/agentCard';
import { publicClient, readAgent, readTotalAgents, type AgentOnChain } from '@/lib/chain';
import { CHAIN_ID, CONTRACTS_CONFIGURED, CONTRACT_ADDRESSES, ERC8004_ABI } from '@/lib/constants';
import { useTx } from './useTx';

export function useTotalAgents() {
  return useQuery({
    queryKey: ['amx', 'totalAgents'],
    queryFn: readTotalAgents,
    enabled: CONTRACTS_CONFIGURED,
    refetchInterval: 30_000,
  });
}

export function useAgent(id: bigint | undefined) {
  return useQuery({
    queryKey: ['amx', 'agent', id?.toString()],
    queryFn: () => readAgent(id as bigint),
    enabled: CONTRACTS_CONFIGURED && id !== undefined,
    refetchInterval: 30_000,
  });
}

/** Newest agents first, capped so the explorer stays one cheap batch of reads. */
export function useAgents(total: number | undefined, limit = 60) {
  return useQuery({
    queryKey: ['amx', 'agents', total, limit],
    enabled: CONTRACTS_CONFIGURED && total !== undefined && total > 0,
    queryFn: async () => {
      const ids: bigint[] = [];
      for (let i = total as number; i >= 1 && ids.length < limit; i--) ids.push(BigInt(i));
      const agents = await Promise.all(ids.map((id) => readAgent(id)));
      return agents.filter((a): a is AgentOnChain => a !== null);
    },
  });
}

export function useAgentCard(uri: string | undefined, agentId: bigint) {
  return useQuery({
    queryKey: ['amx', 'agentCard', agentId.toString(), uri],
    queryFn: () => fetchAgentCard(uri as string, agentId),
    enabled: !!uri,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

/** The caller's latest score for this agent, if any. */
export function useMyFeedback(agentId: bigint, rater: Address | undefined) {
  return useQuery({
    queryKey: ['amx', 'feedback', agentId.toString(), rater],
    enabled: CONTRACTS_CONFIGURED && !!rater,
    queryFn: async () => {
      const [score, exists] = await publicClient.readContract({
        address: CONTRACT_ADDRESSES.ERC8004_IDENTITY,
        abi: ERC8004_ABI,
        functionName: 'getFeedback',
        args: [agentId, rater as Address],
      });
      return exists ? score : null;
    },
  });
}

export function useGiveFeedback() {
  const { writeContractAsync } = useWriteContract();
  const tx = useTx();
  const giveFeedback = (agentId: bigint, score: number, tag: string) =>
    tx.run(() =>
      writeContractAsync({
        address: CONTRACT_ADDRESSES.ERC8004_IDENTITY,
        abi: ERC8004_ABI,
        functionName: 'giveFeedback',
        args: [agentId, score, tag],
        chainId: CHAIN_ID,
      }),
    );
  return { giveFeedback, ...tx };
}