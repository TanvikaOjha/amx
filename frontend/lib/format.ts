import { formatEther } from 'viem';
import { TARGET } from './constants';

export function shortHex(value: string | undefined | null, head = 6, tail = 4): string {
  if (!value) return '—';
  return value.length <= head + tail + 2 ? value : `${value.slice(0, head)}…${value.slice(-tail)}`;
}

/** Amounts in this system are often tiny (default price: 1000 wei), so show wei below 0.000001 ETH. */
export function formatAmount(wei: bigint | undefined | null): string {
  if (wei === undefined || wei === null) return '—';
  if (wei === 0n) return '0 ETH';
  if (wei < 1_000_000_000_000n) return `${wei.toLocaleString('en-US')} wei`;
  const eth = formatEther(wei);
  const [int, frac = ''] = eth.split('.');
  return `${int}${frac ? `.${frac.slice(0, 6).replace(/0+$/, '') || '0'}` : ''} ETH`;
}

export function explorerUrl(kind: 'address' | 'tx' | 'block', value: string): string {
  return `${TARGET.explorer}/${kind}/${value}`;
}

export function formatDuration(totalSeconds: number): string {
  if (totalSeconds <= 0) return '0s';
  const d = Math.floor(totalSeconds / 86400);
  const h = Math.floor((totalSeconds % 86400) / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = Math.floor(totalSeconds % 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function timeAgo(ms: number, now = Date.now()): string {
  const s = Math.max(0, Math.floor((now - ms) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}