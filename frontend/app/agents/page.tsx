'use client';

import { Search } from 'lucide-react';
import { useState } from 'react';
import { AgentCard } from '@/components/AgentCard';
import { EmptyState, Input, Notice } from '@/components/ui';
import { useAgents, useTotalAgents } from '@/hooks/useAgentRegistry';
import { CONTRACTS_CONFIGURED } from '@/lib/constants';
import { errorMessage } from '@/lib/chain';

export default function AgentExplorerPage() {
  const [query, setQuery] = useState('');
  const total = useTotalAgents();
  const count = total.data === undefined ? undefined : Number(total.data);
  const agents = useAgents(count);

  const q = query.trim().toLowerCase();
  const visible = (agents.data ?? []).filter((a) => !q || a.id.toString() === q.replace('#', '') || a.owner.toLowerCase().includes(q) || a.uri.toLowerCase().includes(q));
  const error = total.error ?? agents.error;

  return (
    <div>
      <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-white">Agents</h1>
          <p className="mt-1 text-slate-400">
            {count === undefined ? 'Reading the registry…' : `${count} agent${count === 1 ? '' : 's'} registered on-chain.`}
          </p>
        </div>
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" aria-hidden />
          <Input aria-label="Search agents" placeholder="Search by id, owner or URI" value={query} onChange={(e) => setQuery(e.target.value)} className="pl-9" />
        </div>
      </div>

      {!CONTRACTS_CONFIGURED && <Notice tone="bad">Set the contract addresses in .env.local to load the registry.</Notice>}
      {error && <Notice tone="bad">Could not read the registry: {errorMessage(error)}</Notice>}

      {CONTRACTS_CONFIGURED && !error && count === 0 && (
        <EmptyState title="No agents registered yet">
          Register one with <code className="font-mono text-slate-300">npx hardhat run scripts/register-agent.ts</code> in the contracts folder. It will show up here.
        </EmptyState>
      )}

      {agents.isLoading && count !== 0 && <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">{[0, 1, 2].map((i) => <div key={i} className="h-60 animate-pulse rounded-xl bg-ink-900" />)}</div>}

      {visible.length === 0 && (agents.data?.length ?? 0) > 0 && <EmptyState title="No agent matches that search" />}

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        {visible.map((agent) => (
          <AgentCard key={agent.id.toString()} agent={agent} />
        ))}
      </div>
    </div>
  );
}