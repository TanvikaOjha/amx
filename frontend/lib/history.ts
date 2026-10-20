import { useMemo, useSyncExternalStore } from 'react';
import { CHAIN_ID } from './constants';

export interface HistoryEntry {
  jobId: string;
  task: string;
  agentId: number;
  channelId: string;
  nonce: number;
  charged: string; // wei
  cumulative: string; // wei
  hcsStatus: string;
  hcsTopicId: string | null;
  hcsSequence: number | null;
  replay: boolean;
  at: number;
}

const KEY = `amx:history:${CHAIN_ID}`;
const EVENT = 'amx-history';
const MAX = 50;

function raw(): string {
  try {
    return window.localStorage.getItem(KEY) ?? '[]';
  } catch {
    return '[]';
  }
}

function subscribe(cb: () => void) {
  window.addEventListener(EVENT, cb);
  window.addEventListener('storage', cb);
  return () => {
    window.removeEventListener(EVENT, cb);
    window.removeEventListener('storage', cb);
  };
}

export function pushHistory(entry: HistoryEntry): void {
  try {
    const list = (JSON.parse(raw()) as HistoryEntry[]).filter((e) => !(e.channelId === entry.channelId && e.nonce === entry.nonce));
    window.localStorage.setItem(KEY, JSON.stringify([entry, ...list].slice(0, MAX)));
    window.dispatchEvent(new Event(EVENT));
  } catch {
    /* ignore */
  }
}

/** Executions made from this browser (the gateway has no list endpoint). */
export function useExecutionHistory(): HistoryEntry[] {
  const snapshot = useSyncExternalStore(subscribe, raw, () => '[]');
  return useMemo(() => {
    try {
      const parsed = JSON.parse(snapshot);
      return Array.isArray(parsed) ? (parsed as HistoryEntry[]) : [];
    } catch {
      return [];
    }
  }, [snapshot]);
}