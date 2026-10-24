'use client';

import { Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { AgentCard } from '@/components/AgentCard';
import { Button, EmptyState, Input, Notice, Select } from '@/components/ui';
import { useAgents, useTotalAgents } from '@/hooks/useAgentRegistry';
import { useMyChannels } from '@/hooks/usePaymentChannel';
import { useWalletReady } from '@/hooks/useWalletReady';
import { errorMessage, reputation } from '@/lib/chain';
import { CHANNEL_STATUS, CONTRACTS_CONFIGURED, GATEWAY_AGENT_ID } from '@/lib/constants';

const FILTERS = [
  ['all', 'All'],
  ['rated', 'Rated 80+'],
  ['unrated', 'Unrated'],
  ['mine', 'My open channels'],
  ['served', 'Served by this gateway'],
] as const;
type Filter = (typeof FILTERS)[number][0];
type Sort = 'newest' | 'rating' | 'raters';

export default function AgentExplorerPage() {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [sort, setSort] = useState<Sort>('newest');

  const wallet = useWalletReady();
  const mine = useMyChannels(wallet.address);
  const total = useTotalAgents();
  const count = total.data === undefined ? undefined : Number(total.data);
  const agents = useAgents(count);

  const withChannel = useMemo(
    () => new Set((mine.data ?? []).filter((c) => c.status === CHANNEL_STATUS.Open).map((c) => c.agentId.toString())),
    [mine.data],
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase().replace('#', '');
    const list = (agents.data ?? []).filter((a) => {
      const r = reputation(a);
      if (q && !(a.id.toString() === q || a.owner.toLowerCase().includes(q) || a.uri.toLowerCase().includes(q))) return false;
      if (filter === 'rated') return r.average !== null && r.average >= 80;
      if (filter === 'unrated') return r.count === 0;
      if (filter === 'mine') return withChannel.has(a.id.toString());
      if (filter === 'served') return a.id === GATEWAY_AGENT_ID;
      return true;
    });
    const score = (a: (typeof list)[number]) => reputation(a).average ?? -1;
    return [...list].sort((a, b) =>
      sort === 'rating' ? score(b) - score(a) : sort === 'raters' ? Number(b.clientCount - a.clientCount) : Number(b.id - a.id),
    );
  }, [agents.data, query, filter, sort, withChannel]);

  const error = total.error ?? agents.error;

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight text-white">Agent registry</h1>
        <p className="mt-1 text-slate-400">{count === undefined ? 'Reading the registry…' : `${count} agent${count === 1 ? '' : 's'} registered on-chain.`}</p>
      </div>

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" aria-hidden />
          <Input aria-label="Search agents" placeholder="Search by id, owner or URI" value={query} onChange={(e) => setQuery(e.target.value)} className="pl-9" />
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter agents">
          {FILTERS.map(([key, label]) => (
            <Button key={key} size="sm" variant={filter === key ? 'primary' : 'secondary'} aria-pressed={filter === key} onClick={() => setFilter(key)}>
              {label}
            </Button>
          ))}
        </div>
        <Select aria-label="Sort agents" value={sort} onChange={(e) => setSort(e.target.value as Sort)} className="w-auto sm:ml-auto">
          <option value="newest">Newest first</option>
          <option value="rating">Top rated</option>
          <option value="raters">Most raters</option>
        </Select>
      </div>

      {!CONTRACTS_CONFIGURED && <Notice tone="bad">Set the contract addresses in .env.local to load the registry.</Notice>}
      {error && <Notice tone="bad">Could not read the registry: {errorMessage(error)}</Notice>}
      {filter === 'mine' && !wallet.isConnected && <Notice tone="warn">Connect a wallet to filter by your channels.</Notice>}
      {CONTRACTS_CONFIGURED && !error && count === 0 && <EmptyState title="No agents registered yet">Register one with the script in the contracts folder and it appears here.</EmptyState>}
      {agents.isLoading && count !== 0 && (
        <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-60 animate-pulse rounded-xl bg-ink-900" />
          ))}
        </div>
      )}
      {visible.length === 0 && (agents.data?.length ?? 0) > 0 && (
        <EmptyState
          title="No agent matches"
          action={
            <Button variant="secondary" onClick={() => { setQuery(''); setFilter('all'); }}>
              Clear filters
            </Button>
          }
        />
      )}
      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        {visible.map((a) => (
          <AgentCard key={a.id.toString()} agent={a} />
        ))}
      </div>
    </div>
  );
}