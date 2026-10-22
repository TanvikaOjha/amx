'use client';

import { Activity, Bot, Layers, Wallet } from 'lucide-react';
import Link from 'next/link';
import { useAccount } from 'wagmi';
import { useGatewayHealth } from '@/components/GatewayStatus';
import { Badge, EmptyState, Notice, Panel, PanelTitle, Stat } from '@/components/ui';
import { useTotalAgents } from '@/hooks/useAgentRegistry';
import { useMyChannels, usePending } from '@/hooks/usePaymentChannel';
import { CHANNEL_STATUS, CONTRACTS_CONFIGURED } from '@/lib/constants';
import { formatAmount, shortHex, timeAgo } from '@/lib/format';
import { useExecutionHistory } from '@/lib/history';

export default function DashboardPage() {
  const { address, isConnected } = useAccount();
  const pending = usePending(address);
  const channels = useMyChannels(address);
  const agents = useTotalAgents();
  const health = useGatewayHealth();
  const history = useExecutionHistory();

  const list = channels.data ?? [];
  const open = list.filter((c) => c.status === CHANNEL_STATUS.Open);
  const locked = open.reduce((sum, c) => sum + c.deposit, 0n);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight text-white">Dashboard</h1>
        <p className="mt-1 text-slate-400">Your channels, balances and recent calls, plus the state of the gateway.</p>
      </div>

      {!CONTRACTS_CONFIGURED && <Notice tone="bad">Set the contract addresses in .env.local to load on-chain data.</Notice>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Withdrawable"
          icon={<Wallet className="h-5 w-5" aria-hidden />}
          value={isConnected ? formatAmount(pending.data ?? 0n) : '—'}
          hint={isConnected ? <Link href="/channels" className="text-volt-400 hover:underline">Withdraw on the Channels page</Link> : 'Connect a wallet'}
        />
        <Stat
          label="Open channels"
          icon={<Layers className="h-5 w-5" aria-hidden />}
          value={isConnected ? open.length : '—'}
          hint={isConnected ? `${formatAmount(locked)} locked in deposits` : 'Connect a wallet'}
        />
        <Stat
          label="Registered agents"
          icon={<Bot className="h-5 w-5" aria-hidden />}
          value={agents.data === undefined ? '—' : Number(agents.data)}
          hint={<Link href="/agents" className="text-volt-400 hover:underline">Browse the registry</Link>}
        />
        <Stat
          label="Gateway"
          icon={<Activity className="h-5 w-5" aria-hidden />}
          value={health.isLoading ? '…' : health.data ? (health.data.status === 'ok' ? 'Online' : 'Degraded') : 'Offline'}
          hint={health.data ? `Agent #${health.data.agentId} on ${health.data.network}` : 'Could not reach /health'}
        />
      </div>

      {health.data && (
        <Panel>
          <PanelTitle>Gateway health</PanelTitle>
          <div className="flex flex-wrap gap-3 text-sm">
            <Badge tone={health.data.registry === 'ok' ? 'good' : 'bad'}>Registry: {health.data.registry}</Badge>
            <Badge tone={health.data.hcs === 'ok' ? 'good' : health.data.hcs === 'disabled' ? 'neutral' : 'warn'}>Hedera audit: {health.data.hcs}</Badge>
          </div>
          {health.data.hcs.startsWith('pending') && <p className="mt-3 text-sm text-slate-400">Proofs are queued and will retry automatically.</p>}
        </Panel>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-100">Recent calls from this browser</h2>
        {history.length === 0 ? (
          <EmptyState title="No calls yet" action={<Link href="/agents" className="font-semibold text-volt-400 underline">Run your first task</Link>}>
            Calls you make here are listed with their payment and audit receipts.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-slate-800 bg-ink-900/70">
            <table className="w-full min-w-[40rem] text-left text-sm">
              <thead className="border-b border-slate-800 text-xs text-slate-400">
                <tr>
                  <th className="p-3 font-medium">When</th>
                  <th className="p-3 font-medium">Task</th>
                  <th className="p-3 font-medium">Agent</th>
                  <th className="p-3 font-medium">Voucher</th>
                  <th className="p-3 font-medium">Charged</th>
                  <th className="p-3 font-medium">Audit proof</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {history.slice(0, 10).map((h) => (
                  <tr key={`${h.channelId}-${h.nonce}`}>
                    <td className="p-3 text-slate-400">{timeAgo(h.at)}</td>
                    <td className="p-3 font-mono text-xs">{h.task}</td>
                    <td className="p-3">
                      <Link href={`/agents/${h.agentId}`} className="text-volt-400 hover:underline">#{h.agentId}</Link>
                    </td>
                    <td className="p-3 font-mono text-xs">{shortHex(h.channelId, 6, 4)} · {h.nonce}</td>
                    <td className="p-3 font-mono text-xs">{formatAmount(BigInt(h.charged))}</td>
                    <td className="p-3">
                      {h.hcsSequence !== null ? <Badge tone="good">#{h.hcsSequence}</Badge> : <Badge tone={h.hcsStatus === 'pending' ? 'warn' : 'neutral'}>{h.hcsStatus}</Badge>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
