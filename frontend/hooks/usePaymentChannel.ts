'use client';

import { useQuery } from '@tanstack/react-query';
import { useReadContract, useWriteContract } from 'wagmi';
import { parseEventLogs, type Address, type Hex } from 'viem';
import { fetchOpenedChannelIds, loadKnownChannels, readChannel, rememberChannel, type ChannelView } from '@/lib/chain';
import { CHAIN_ID, CONTRACTS_CONFIGURED, CONTRACT_ADDRESSES, PAYMENT_CHANNEL_ABI } from '@/lib/constants';
import { fetchLedger } from '@/lib/gateway';
import { useTx } from './useTx';

export function usePending(address: Address | undefined) {
  return useReadContract({
    address: CONTRACT_ADDRESSES.PAYMENT_CHANNEL,
    abi: PAYMENT_CHANNEL_ABI,
    functionName: 'pending',
    args: address ? [address] : undefined,
    chainId: CHAIN_ID,
    query: { enabled: CONTRACTS_CONFIGURED && !!address, refetchInterval: 15_000 },
  });
}

export function useMinChallengePeriod() {
  return useReadContract({
    address: CONTRACT_ADDRESSES.PAYMENT_CHANNEL,
    abi: PAYMENT_CHANNEL_ABI,
    functionName: 'minChallengePeriod',
    chainId: CHAIN_ID,
    query: { enabled: CONTRACTS_CONFIGURED },
  });
}

/** Every channel this account opened (logs + ones remembered locally), with live on-chain state, newest first. */
export function useMyChannels(address: Address | undefined) {
  return useQuery({
    queryKey: ['amx', 'channels', address?.toLowerCase()],
    enabled: CONTRACTS_CONFIGURED && !!address,
    refetchInterval: 20_000,
    queryFn: async (): Promise<ChannelView[]> => {
      const payer = address as Address;
      let fromLogs: Hex[] = [];
      try {
        fromLogs = await fetchOpenedChannelIds(payer);
      } catch {
        /* RPC refused the log query: fall back to channels remembered in this browser */
      }
      const ids = Array.from(new Set([...fromLogs, ...loadKnownChannels(payer)]));
      const channels = await Promise.all(ids.map((id) => readChannel(id)));
      return channels
        .filter((c): c is ChannelView => c !== null && c.payer.toLowerCase() === payer.toLowerCase())
        .reverse();
    },
  });
}

/** The gateway's own ledger for a channel (what the NEXT voucher must satisfy). */
export function useLedger(channelId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['amx', 'ledger', channelId],
    enabled: enabled && !!channelId,
    queryFn: () => fetchLedger(channelId as string),
    refetchInterval: 15_000,
  });
}

const base = { address: CONTRACT_ADDRESSES.PAYMENT_CHANNEL, abi: PAYMENT_CHANNEL_ABI, chainId: CHAIN_ID } as const;

export function useChannelActions(payer: Address | undefined) {
  const { writeContractAsync } = useWriteContract();
  const tx = useTx();

  const openChannel = async (agentId: bigint, challengePeriod: bigint, value: bigint): Promise<Hex | null> => {
    const receipt = await tx.run(() => writeContractAsync({ ...base, functionName: 'openChannel', args: [agentId, challengePeriod], value }));
    if (!receipt) return null;
    const [opened] = parseEventLogs({ abi: PAYMENT_CHANNEL_ABI, logs: receipt.logs, eventName: 'ChannelOpened' });
    const id = opened?.args.channelId ?? null;
    if (id && payer) rememberChannel(payer, id);
    return id;
  };

  return {
    ...tx,
    openChannel,
    topUp: (channelId: Hex, value: bigint) => tx.run(() => writeContractAsync({ ...base, functionName: 'topUp', args: [channelId], value })),
    startClose: (channelId: Hex) => tx.run(() => writeContractAsync({ ...base, functionName: 'startClose', args: [channelId] })),
    finalizeClose: (channelId: Hex) => tx.run(() => writeContractAsync({ ...base, functionName: 'finalizeClose', args: [channelId] })),
    withdraw: () => tx.run(() => writeContractAsync({ ...base, functionName: 'withdraw' })),
  };
}