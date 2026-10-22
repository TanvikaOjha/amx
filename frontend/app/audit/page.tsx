'use client';

import { ExternalLink, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { HCSFeedTable } from '@/components/HCSFeedTable';
import { Button, Field, Input, Notice, Panel } from '@/components/ui';
import { useHcsFeed } from '@/hooks/useHcsFeed';
import { HCS_TOPIC_ID, HEDERA_NETWORK } from '@/lib/constants';
import { hashscanTopicUrl } from '@/lib/hedera';
import { useExecutionHistory } from '@/lib/history';

export default function AuditPage() {
  const history = useExecutionHistory();
  const fromHistory = history.find((h) => h.hcsTopicId)?.hcsTopicId ?? undefined;
  const [topicInput, setTopicInput] = useState(HCS_TOPIC_ID ?? '');
  const [seqInput, setSeqInput] = useState('');
  const [auto, setAuto] = useState(true);

  const topic = /^\d+\.\d+\.\d+$/.test(topicInput.trim()) ? topicInput.trim() : (HCS_TOPIC_ID ?? fromHistory);
  const sequence = /^\d+$/.test(seqInput) && Number(seqInput) > 0 ? Number(seqInput) : undefined;
  const feed = useHcsFeed(topic, { limit: 25, sequence, refetchMs: auto && sequence === undefined ? 8000 : false });

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-white">Audit log</h1>
          <p className="mt-1 max-w-2xl text-slate-400">
            Proofs the gateway wrote to Hedera Consensus Service ({HEDERA_NETWORK}). Each signature is checked here against the agent’s on-chain signing key, so you do not have to trust the mirror node.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={() => feed.refetch()} loading={feed.isFetching}>
            <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Refresh
          </Button>
          {topic && (
            <a
              href={hashscanTopicUrl(topic)}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-ink-800 px-3 py-1.5 text-xs font-semibold text-slate-100 hover:border-slate-500"
            >
              HashScan <ExternalLink className="h-3.5 w-3.5" aria-hidden />
            </a>
          )}
        </div>
      </div>

      <Panel>
        <div className="grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <Field label="Topic id" hint={HCS_TOPIC_ID ? 'Default from NEXT_PUBLIC_HCS_TOPIC_ID.' : 'Set NEXT_PUBLIC_HCS_TOPIC_ID, or paste the TOPIC_ID from create_topic.py.'}>
            <Input value={topicInput} onChange={(e) => setTopicInput(e.target.value)} placeholder={fromHistory ?? '0.0.1234567'} className="font-mono" />
          </Field>
          <Field label="Look up one sequence number" hint="Leave empty to show the latest 25.">
            <Input inputMode="numeric" value={seqInput} onChange={(e) => setSeqInput(e.target.value.trim())} placeholder="e.g. 42" className="font-mono" />
          </Field>
          <label className="flex items-center gap-2 pb-2 text-sm text-slate-300">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} className="accent-cyan-400" />
            Auto-refresh
          </label>
        </div>
      </Panel>

      {!topic && <Notice tone="warn">No topic yet. Create one with <code className="font-mono">python scripts/create_topic.py</code> in the gateway folder, then paste its id above.</Notice>}
      {feed.isError && <Notice tone="bad">Could not read the mirror node: {feed.error instanceof Error ? feed.error.message : 'unknown error'}</Notice>}
      {feed.isLoading && topic && <div className="h-48 animate-pulse rounded-xl bg-ink-900" />}
      {feed.data && <HCSFeedTable rows={feed.data} />}
    </div>
  );
}
