'use client';

import { ArrowDownToLine, Plus } from 'lucide-react';
import { useMemo, useState } from 'react';
import { isHex, parseEther, type Hex } from 'viem';
import { useQueryClient } from '@tanstack/react-query';
import { PaymentChannelCard } from '@/components/PaymentChannelCard';
import { Button, EmptyState, Field, Input, Notice, Panel, PanelTitle, Select, Stat, TxStatus } from '@/components/ui';
import { useChannelActions, useMinChallengePeriod, useMyChannels, usePending } from '@/hooks/usePaymentChannel';
import { useWalletReady } from '@/hooks/useWalletReady';
import { errorMessage, readChannel, rememberChannel } from '@/lib/chain';
import { CHANNEL_STATUS, GATEWAY_AGENT_ID } from '@/lib/constants';
import { formatAmount, formatDuration } from '@/lib/format';

const PERIODS = [3600, 6 * 3600, 24 * 3600, 7 * 86400];
const TABS = [
  ['all', 'All'],
  ['open', 'Open'],
  ['closing', 'Closing'],
  ['closed', 'Closed'],
] as const;
type Tab = (typeof TABS)[number][0];
const STATUS_OF: Record<Exclude<Tab, 'all'>, number> = {
  open: CHANNEL_STATUS.Open,
  closing: CHANNEL_STATUS.Closing,
  closed: CHANNEL_STATUS.Closed,
};

export default function ChannelsPage() {
  const wallet = useWalletReady();
  const queryClient = useQueryClient();
  const channels = useMyChannels(wallet.address);
  const pending = usePending(wallet.address);
  const minPeriod = useMinChallengePeriod();
  const actions = useChannelActions(wallet.address);
  const withdrawal = useChannelActions(wallet.address); // separate status line for withdraw

  const [tab, setTab] = useState<Tab>('all');
  const [agentIdInput, setAgentIdInput] = useState(GATEWAY_AGENT_ID.toString());
  const [deposit, setDeposit] = useState('0.0001');
  const [period, setPeriod] = useState(3600);
  const [addId, setAddId] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [openedId, setOpenedId] = useState<Hex | null>(null);

  const all = useMemo(() => channels.data ?? [], [channels.data]);
  const shown = tab === 'all' ? all : all.filter((c) => c.status === STATUS_OF[tab]);
  const openCount = all.filter((c) => c.status === CHANNEL_STATUS.Open).length;
  const escrow = all.filter((c) => c.status !== CHANNEL_STATUS.Closed).reduce((s, c) => s + c.deposit, 0n);
  const claimed = all.filter((c) => c.status === CHANNEL_STATUS.Closing).reduce((s, c) => s + c.claimed, 0n);
  const readyToFinalize = all.filter((c) => c.status === CHANNEL_STATUS.Closing && Number(c.closeDeadline) * 1000 <= Date.now()).length;

  const min = Number(minPeriod.data ?? 3600n);
  const periods = PERIODS.filter((p) => p >= min);
  const effectivePeriod = periods.includes(period) ? period : (periods[0] ?? min);

  let depositWei: bigint | null = null;
  try {
    depositWei = parseEther(deposit || '0');
  } catch {
    depositWei = null;
  }
  const agentOk = /^\d{1,18}$/.test(agentIdInput) && BigInt(agentIdInput) > 0n;
  const canOpen = wallet.ready && agentOk && depositWei !== null && depositWei > 0n;
  const pendingWei = pending.data ?? 0n;

  async function open() {
    setOpenedId(null);
    const id = await actions.openChannel(BigInt(agentIdInput), BigInt(effectivePeriod), depositWei as bigint);
    if (id) setOpenedId(id);
  }

  async function addExisting() {
    setAddError(null);
    const id = addId.trim().toLowerCase() as Hex;
    if (!isHex(id) || id.length !== 66) return setAddError('A channel id is 0x followed by 64 hex characters.');
    try {
      const ch = await readChannel(id);
      if (!ch) return setAddError('No channel with that id exists on this network.');
      if (!wallet.address || ch.payer.toLowerCase() !== wallet.address.toLowerCase()) return setAddError('That channel was opened by a different account.');
      rememberChannel(wallet.address, id);
      setAddId('');
      await queryClient.invalidateQueries({ queryKey: ['amx', 'channels'] });
    } catch (e) {
      setAddError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight text-white">Payment channels</h1>
        <p className="mt-1 max-w-2xl text-slate-400">
          Each channel is a deposit locked for one agent. Calls are signed vouchers against it; only the final total settles on-chain.
        </p>
      </div>

      {!wallet.isConnected && <Notice tone="warn">Connect a wallet to open and manage channels.</Notice>}
      {wallet.wrongNetwork && <Notice tone="warn">Switch your wallet to the app’s network first.</Notice>}

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="In escrow" value={wallet.isConnected ? formatAmount(escrow) : '—'} hint={`${openCount} open channel${openCount === 1 ? '' : 's'}`} />
        <Stat
          label="Claimed during closes"
          value={wallet.isConnected ? formatAmount(claimed) : '—'}
          hint={readyToFinalize ? `${readyToFinalize} ready to finalize` : 'Agents submit vouchers inside the window'}
        />
        <Stat label="Withdrawable" value={wallet.isConnected ? formatAmount(pendingWei) : '—'} hint="Refunds and payouts from settled channels" />
      </div>

      <div className="grid gap-8 lg:grid-cols-2">
        <Panel>
          <PanelTitle icon={<Plus className="h-5 w-5" aria-hidden />}>Open a channel</PanelTitle>
          <div className="space-y-4">
            <Field label="Agent id" hint={`The gateway in this app serves agent #${GATEWAY_AGENT_ID}.`}>
              <Input inputMode="numeric" value={agentIdInput} onChange={(e) => setAgentIdInput(e.target.value.trim())} />
            </Field>
            <Field label="Deposit (ETH)" hint="Test ETH only. Unused funds come back when the channel closes.">
              <Input inputMode="decimal" value={deposit} onChange={(e) => setDeposit(e.target.value)} className="font-mono" />
            </Field>
            <Field label="Challenge window" hint="After you start a close, the agent has this long to submit its latest voucher. The gateway rejects windows under 1 hour.">
              <Select value={effectivePeriod} onChange={(e) => setPeriod(Number(e.target.value))}>
                {periods.map((p) => (
                  <option key={p} value={p}>
                    {formatDuration(p)}
                  </option>
                ))}
              </Select>
            </Field>
            <Button onClick={open} loading={actions.busy} disabled={!canOpen}>
              Deposit and open channel
            </Button>
            <TxStatus state={actions.state} />
            {openedId && (
              <Notice tone="good">
                Channel opened: <span className="break-all font-mono text-xs">{openedId}</span>
              </Notice>
            )}
          </div>
        </Panel>

        <Panel className="flex flex-col justify-between">
          <div>
            <PanelTitle icon={<ArrowDownToLine className="h-5 w-5" aria-hidden />}>Withdraw</PanelTitle>
            <p className="text-sm text-slate-400">
              Settling a channel credits a balance inside the contract. Funds only reach your wallet when you withdraw. If you own an agent, its payouts wait here too.
            </p>
            <div className="my-6 font-mono text-4xl font-semibold tabular-nums text-white">{wallet.isConnected ? formatAmount(pendingWei) : '—'}</div>
          </div>
          <div className="space-y-3">
            <Button onClick={() => withdrawal.withdraw()} loading={withdrawal.busy} disabled={!wallet.ready || pendingWei === 0n}>
              Withdraw to wallet
            </Button>
            <TxStatus state={withdrawal.state} />
          </div>
        </Panel>
      </div>

      <section className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-1.5" role="group" aria-label="Filter channels by status">
            {TABS.map(([key, label]) => (
              <Button key={key} size="sm" variant={tab === key ? 'primary' : 'secondary'} aria-pressed={tab === key} onClick={() => setTab(key)}>
                {label}
                {key !== 'all' && <span className="font-mono opacity-70">{all.filter((c) => c.status === STATUS_OF[key]).length}</span>}
              </Button>
            ))}
          </div>
          {wallet.isConnected && (
            <div className="flex w-full gap-2 sm:w-auto">
              <Input aria-label="Channel id to add" placeholder="Add a channel by id (0x…)" value={addId} onChange={(e) => setAddId(e.target.value)} className="font-mono sm:w-80" />
              <Button variant="secondary" onClick={addExisting} disabled={!addId}>
                Add
              </Button>
            </div>
          )}
        </div>
        {addError && <Notice tone="bad">{addError}</Notice>}
        {channels.isError && (
          <Notice tone="warn">
            Could not scan the chain for your channels ({errorMessage(channels.error)}). Channels opened here are still remembered; set NEXT_PUBLIC_DEPLOY_BLOCK or a dedicated RPC URL to improve discovery.
          </Notice>
        )}
        {channels.isLoading && <div className="h-40 animate-pulse rounded-xl bg-ink-900" />}
        {wallet.isConnected && !channels.isLoading && shown.length === 0 && (
          <EmptyState title={all.length ? 'No channels in this state' : 'No channels yet'}>
            {all.length ? 'Try another tab.' : 'Open one above to start paying for agent calls.'}
          </EmptyState>
        )}
        <div className="grid gap-6 md:grid-cols-2">
          {shown.map((c) => (
            <PaymentChannelCard key={c.id} channel={c} />
          ))}
        </div>
      </section>
    </div>
  );
}