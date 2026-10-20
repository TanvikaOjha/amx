import { getDefaultConfig } from '@rainbow-me/rainbowkit';
import { http, type Chain, type Transport } from 'viem';
import { CHAIN_ID, SUPPORTED_CHAINS, WALLETCONNECT_PROJECT_ID, type SupportedChainId } from './constants';

// Target chain first so it is the default selection.
const otherId: SupportedChainId = CHAIN_ID === 84532 ? 11155111 : 84532;
const chains = [SUPPORTED_CHAINS[CHAIN_ID].chain, SUPPORTED_CHAINS[otherId].chain] as unknown as readonly [Chain, ...Chain[]];

const transports: Record<number, Transport> = {
  84532: http(process.env.NEXT_PUBLIC_BASE_SEPOLIA_RPC_URL || undefined),
  11155111: http(process.env.NEXT_PUBLIC_SEPOLIA_RPC_URL || undefined),
};

export const config = getDefaultConfig({
  appName: 'Agent-Mesh-X',
  projectId: WALLETCONNECT_PROJECT_ID,
  chains,
  transports,
  ssr: true,
});

