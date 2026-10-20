'use client';

import { ArrowDownToLine, Plus } from 'lucide-react';
import { useState } from 'react';
import { isHex, parseEther, type Hex } from 'viem';
import { PaymentChannelCard } from '@/components/PaymentChannelCard';
import { Button, EmptyState, Field, Input, Notice, Panel, PanelTitle, Select, TxStatus } from '@/components/ui';
import { useChannelActions, useMinChallengePeriod, useMyChannels, usePending } from '@/hooks/usePaymentChannel';
import { useWalletReady } from '@/hooks/useWalletReady';
import { errorMessage, readChannel, rememberChannel } from '@/lib/chain';
import { GATEWAY_AGENT_ID } from '@/lib/constants';
import { formatAmount, formatDuration } from '@/lib/format';
import { useQueryClient } from '@tanstack/react-query';

const PERIODS = [3600, 6 * 3600, 24 * 3600, 7 * 86400];

export default function ChannelsPage() {
  const wallet = useWalletReady();
  const queryClient = useQueryClient();
  const channels = useMyChannels(wallet.address);
  const pending = usePending(wallet.address);
  const minPeriod = useMinChallengePeriod();
  const actions = useChannelActions(wallet.address);
  const withdrawal = useChannelActions(wallet.address); // separate status line for withdraw

  const [agentIdInput, setAgentIdInput] = useState(GATEWAY_AGENT_ID.toString());
  const [deposit, setDeposit] = useState('0.0001');
  const [period, setPeriod] = useState(3600);
  const [addId, setAddId] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [openedId, setOpenedId] = useState<Hex | null>(null);

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

  const pendingWei = pending.data ?? 0n;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight text-white">Payment channels</h1>
        <p className="mt-1 max-w-2xl text-slate-400">
          A channel is a deposit locked in the contract for one agent. Each call you make is a signed voucher against that deposit; only the final total is ever settled on-chain.
        </p>
      </div>

      {!wallet.isConnected && <Notice tone="warn">Connect a wallet to open and manage channels.</Notice>}
      {wallet.wrongNetwork && <Notice tone="warn">Switch your wallet to the app’s network first.</Notice>}

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
                Channel opened: <span className="break-all font-mono text-xs">{openedId}</span>. It is listed below, and you can pay from it on the agent page.
              </Notice>
            )}
          </div>
        </Panel>

        <Panel className="flex flex-col justify-between">
          <div>
            <PanelTitle icon={<ArrowDownToLine className="h-5 w-5" aria-hidden />}>Withdrawable balance</PanelTitle>
            <p className="text-sm text-slate-400">Refunds from closed channels (or payouts, if you own an agent) wait here until you withdraw them.</p>
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
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h2 className="text-lg font-semibold text-slate-100">Your channels</h2>
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
        {channels.isError && <Notice tone="warn">Could not scan the chain for your channels ({errorMessage(channels.error)}). Channels you open here are still remembered; set NEXT_PUBLIC_DEPLOY_BLOCK or a dedicated RPC URL to improve discovery.</Notice>}
        {channels.isLoading && <div className="h-40 animate-pulse rounded-xl bg-ink-900" />}
        {wallet.isConnected && !channels.isLoading && (channels.data?.length ?? 0) === 0 && <EmptyState title="No channels yet">Open one above to start paying for agent calls.</EmptyState>}
        <div className="grid gap-6 md:grid-cols-2">
          {(channels.data ?? []).map((c) => (
            <PaymentChannelCard key={c.id} channel={c} />
          ))}
        </div>
      </section>
    </div>
  );
}