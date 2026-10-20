'use client';

import { ArrowLeft, Bot, Shield } from 'lucide-react';
import Link from 'next/link';
import { use } from 'react';
import { ReputationBadge } from '@/components/AgentCard';
import { ExecuteTaskCard } from '@/components/ExecuteTaskCard';
import { FeedbackForm } from '@/components/FeedbackForm';
import { Badge, CopyText, EmptyState, HexLink, KeyValue, Notice, Panel, PanelTitle } from '@/components/ui';
import { useAgent, useAgentCard } from '@/hooks/useAgentRegistry';
import { errorMessage, reputation } from '@/lib/chain';
import { CONTRACTS_CONFIGURED, GATEWAY_AGENT_ID } from '@/lib/constants';
import { formatAmount } from '@/lib/format';

const ZERO = '0x0000000000000000000000000000000000000000';

export default function AgentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const valid = /^\d{1,18}$/.test(id) && BigInt(id) > 0n;
  const agentId = valid ? BigInt(id) : 0n;

  const agent = useAgent(valid ? agentId : undefined);
  const card = useAgentCard(agent.data?.uri, agentId);

  if (!valid) return <EmptyState title="That is not a valid agent id" action={<Link className="text-volt-400 underline" href="/agents">Back to agents</Link>} />;
  if (!CONTRACTS_CONFIGURED) return <Notice tone="bad">Set the contract addresses in .env.local to load agents.</Notice>;
  if (agent.isLoading) return <div className="h-64 animate-pulse rounded-xl bg-ink-900" />;
  if (agent.error) return <Notice tone="bad">Could not read the registry: {errorMessage(agent.error)}</Notice>;
  if (!agent.data) return <EmptyState title={`Agent #${id} does not exist`} action={<Link className="text-volt-400 underline" href="/agents">Back to agents</Link>} />;

  const a = agent.data;
  const served = agentId === GATEWAY_AGENT_ID;
  const rep = reputation(a);

  return (
    <div className="space-y-8">
      <Link href="/agents" className="inline-flex items-center gap-1.5 text-sm text-slate-400 hover:text-white">
        <ArrowLeft className="h-4 w-4" aria-hidden /> All agents
      </Link>

      <div className="flex flex-wrap items-center gap-4">
        <div className="rounded-xl border border-volt-500/30 bg-volt-500/10 p-3 text-volt-400">
          <Bot className="h-8 w-8" aria-hidden />
        </div>
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-white">{card.data?.name ?? `Agent #${id}`}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Badge tone="info">ERC-8004 #{id}</Badge>
            {served && <Badge tone="good">Served by this gateway</Badge>}
            {a.signer === ZERO && <Badge tone="warn">No signing key registered</Badge>}
          </div>
        </div>
      </div>
      {card.data?.description && <p className="max-w-2xl text-slate-400">{card.data.description}</p>}

      <div className="grid gap-8 lg:grid-cols-2">
        <div className="space-y-8">
          {served ? (
            <ExecuteTaskCard agentId={agentId} />
          ) : (
            <Panel>
              <PanelTitle>Run a task</PanelTitle>
              <Notice>The configured gateway serves agent #{GATEWAY_AGENT_ID.toString()}. This agent has no gateway connected to this app.</Notice>
            </Panel>
          )}
        </div>

        <div className="space-y-8">
          <Panel>
            <PanelTitle icon={<Shield className="h-5 w-5" aria-hidden />} aside={<FeedbackForm agentId={agentId} owner={a.owner} />}>
              Reputation
            </PanelTitle>
            <div className="flex items-baseline gap-3">
              <span className="font-mono text-4xl font-semibold tabular-nums text-white">{rep.average === null ? '—' : rep.average.toFixed(0)}</span>
              {rep.average !== null && <span className="text-slate-500">/ 100</span>}
              <span className="ml-auto text-sm text-slate-400">
                {rep.count} rater{rep.count === 1 ? '' : 's'}
              </span>
            </div>
            {rep.average !== null && (
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-ink-700" role="img" aria-label={`Average score ${rep.average.toFixed(0)} out of 100`}>
                <div className="h-full rounded-full bg-amber-400" style={{ width: `${rep.average}%` }} />
              </div>
            )}
            <p className="mt-3 text-xs text-slate-500">Each wallet counts once; rating again replaces its earlier score.</p>
          </Panel>

          <Panel>
            <PanelTitle>On-chain identity</PanelTitle>
            <dl>
              <KeyValue label="Owner (payee)">
                <HexLink kind="address" value={a.owner} />
              </KeyValue>
              <KeyValue label="Signing address">{a.signer === ZERO ? <span className="text-amber-300">not set</span> : <HexLink kind="address" value={a.signer} />}</KeyValue>
              <KeyValue label="Public key">{a.pubKey === '0x' ? <span className="text-amber-300">not set</span> : <CopyText value={a.pubKey} />}</KeyValue>
              <KeyValue label="Registration URI">
                <span className="break-all font-mono text-xs">{a.uri || '—'}</span>
              </KeyValue>
              <KeyValue label="Ratings">
                <ReputationBadge agent={a} />
              </KeyValue>
            </dl>
            <p className="mt-3 text-xs text-slate-500">The signing address is derived on-chain from the public key. Audit proofs must be signed by it.</p>
          </Panel>

          <Panel>
            <PanelTitle>Registration file</PanelTitle>
            {card.isLoading && <div className="h-16 animate-pulse rounded bg-ink-800" />}
            {card.isError && <Notice tone="warn">The registration file could not be loaded. It may be offline or may block browser requests.</Notice>}
            {card.data && (
              <dl>
                {card.data.amx?.pricePerCall && (
                  <KeyValue label="Advertised price">
                    <span className="font-mono">{formatAmount(BigInt(card.data.amx.pricePerCall))}</span> per call
                  </KeyValue>
                )}
                {card.data.amx?.network && <KeyValue label="Payment network">{card.data.amx.network}</KeyValue>}
                {card.data.services.map((s) => (
                  <KeyValue key={s.endpoint} label={s.name}>
                    <span className="break-all font-mono text-xs">{s.endpoint}</span>
                  </KeyValue>
                ))}
                {card.data.supportedTrust.length > 0 && <KeyValue label="Trust models">{card.data.supportedTrust.join(', ')}</KeyValue>}
              </dl>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}