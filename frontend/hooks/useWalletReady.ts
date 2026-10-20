'use client';

import { useAccount, useSwitchChain } from 'wagmi';
import { CHAIN_ID, CONTRACTS_CONFIGURED } from '@/lib/constants';

/** Connection + network state every write/sign action needs. */
export function useWalletReady() {
  const { address, isConnected, chainId } = useAccount();
  const { switchChain, isPending: switching } = useSwitchChain();
  const wrongNetwork = isConnected && chainId !== CHAIN_ID;
  return {
    address,
    isConnected,
    wrongNetwork,
    switching,
    switchToTarget: () => switchChain({ chainId: CHAIN_ID }),
    contractsConfigured: CONTRACTS_CONFIGURED,
    ready: isConnected && !wrongNetwork && CONTRACTS_CONFIGURED,
  };
}