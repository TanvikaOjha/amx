import { CONTRACTS_CONFIGURED, CONTRACT_ADDRESSES, HCS_TOPIC_ID, TARGET } from '@/lib/constants';
import { explorerUrl, shortHex } from '@/lib/format';
import { hashscanTopicUrl } from '@/lib/hedera';

export function Footer() {
  const link = 'text-slate-400 hover:text-white hover:underline';
  return (
    <footer className="mt-24 border-t border-slate-800">
      <div className="mx-auto grid max-w-7xl gap-8 px-4 py-10 text-sm sm:px-6 md:grid-cols-3">
        <div>
          <p className="font-semibold text-slate-200">Agent-Mesh-X</p>
          <p className="mt-2 max-w-xs text-slate-500">Pay-per-call AI agents: ERC-8004 identity, HTTP 402 vouchers, Hedera audit proofs. Testnet only.</p>
        </div>
        <div>
          <p className="font-semibold text-slate-200">Contracts on {TARGET.label}</p>
          {CONTRACTS_CONFIGURED ? (
            <ul className="mt-2 space-y-1.5 font-mono text-xs">
              <li>
                <a className={link} href={explorerUrl('address', CONTRACT_ADDRESSES.ERC8004_IDENTITY)} target="_blank" rel="noopener noreferrer">
                  ERC8004Identity {shortHex(CONTRACT_ADDRESSES.ERC8004_IDENTITY)}
                </a>
              </li>
              <li>
                <a className={link} href={explorerUrl('address', CONTRACT_ADDRESSES.PAYMENT_CHANNEL)} target="_blank" rel="noopener noreferrer">
                  PaymentChannel {shortHex(CONTRACT_ADDRESSES.PAYMENT_CHANNEL)}
                </a>
              </li>
            </ul>
          ) : (
            <p className="mt-2 text-slate-500">Not configured.</p>
          )}
        </div>
        <div>
          <p className="font-semibold text-slate-200">Protocol</p>
          <ul className="mt-2 space-y-1.5">
            <li>
              <a className={link} href="https://eips.ethereum.org/EIPS/eip-8004" target="_blank" rel="noopener noreferrer">
                ERC-8004 specification
              </a>
            </li>
            <li>
              <a className={link} href="https://eips.ethereum.org/EIPS/eip-712" target="_blank" rel="noopener noreferrer">
                EIP-712 typed data
              </a>
            </li>
            {HCS_TOPIC_ID && (
              <li>
                <a className={link} href={hashscanTopicUrl(HCS_TOPIC_ID)} target="_blank" rel="noopener noreferrer">
                  Audit topic {HCS_TOPIC_ID} on HashScan
                </a>
              </li>
            )}
          </ul>
        </div>
      </div>
    </footer>
  );
}
