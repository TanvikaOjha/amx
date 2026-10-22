'use client';

import { ArrowUpRight, Bot, Star } from 'lucide-react';
import Link from 'next/link';
import { useAgentCard } from '@/hooks/useAgentRegistry';
import { reputation, type AgentOnChain } from '@/lib/chain';
import { GATEWAY_AGENT_ID } from '@/lib/constants';
import { cn } from '@/lib/utils';
import { Badge, CopyText, Panel } from './ui';

export function ReputationBadge({ agent }: { agent: Pick<AgentOnChain, 'clientCount' | 'scoreSum'> }) {
  const { average, count } = reputation(agent);
  if (average === null) return <span className="text-xs text-slate-500">No ratings yet</span>;
  return (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-amber-300" title={`${count} rater${count === 1 ? '' : 's'}, scores 0–100`}>
      <Star className="h-3.5 w-3.5 fill-amber-300" aria-hidden />
      {average.toFixed(0)}/100
      <span className="font-normal text-slate-500">({count})</span>
    </span>
  );
}

/** ERC-8004 identity summary. The name and description come from the agent's off-chain registration file. */
export function AgentCard({ agent }: { agent: AgentOnChain }) {
  const card = useAgentCard(agent.uri, agent.id);
  const served = agent.id === GATEWAY_AGENT_ID;
  const missingKey = agent.signer === '0x0000000000000000000000000000000000000000';

  return (
    <Panel className="flex flex-col justify-between transition-colors hover:border-volt-600/60">
      <div>
        <div className="mb-4 flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-md bg-volt-500/10 px-2 py-1 font-mono text-xs text-volt-300">
            <Bot className="h-3.5 w-3.5" aria-hidden /> Agent #{agent.id.toString()}
          </span>
          <ReputationBadge agent={agent} />
        </div>
        <h3 className="text-lg font-semibold text-slate-50">
          {card.data?.name ?? (card.isLoading ? <span className="inline-block h-5 w-40 animate-pulse rounded bg-ink-700" /> : `Agent ${agent.id}`)}
        </h3>
        <p className="mt-1 line-clamp-2 min-h-10 text-sm text-slate-400">
          {card.data?.description ?? (card.isError ? 'Registration file unavailable.' : '')}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {served && <Badge tone="good">Served by this gateway</Badge>}
          {missingKey && <Badge tone="warn">No signing key</Badge>}
          {card.data?.active === false && <Badge tone="warn">Inactive</Badge>}
        </div>
        <dl className="mt-4 space-y-1 text-xs text-slate-500">
          <div className="flex justify-between gap-3">
            <dt>Owner</dt>
            <dd>
              <CopyText value={agent.owner} />
            </dd>
          </div>
        </dl>
      </div>
      <Link
        href={`/agents/${agent.id}`}
        className={cn(
          'mt-5 flex items-center justify-center gap-2 rounded-lg bg-ink-800 px-4 py-2 text-sm font-semibold text-slate-100 transition-colors hover:bg-ink-700',
          'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-volt-400',
        )}
      >
        {served ? 'Inspect and run a task' : 'Inspect agent'}
        <ArrowUpRight className="h-4 w-4" aria-hidden />
      </Link>
    </Panel>
  );
}
