'use client';

import { Layers } from 'lucide-react';
import { useState } from 'react';
import { parseEther } from 'viem';
import { useChannelActions, useLedger } from '@/hooks/usePaymentChannel';
import { useNow } from '@/hooks/useNow';
import { useWalletReady } from '@/hooks/useWalletReady';
import type { ChannelView } from '@/lib/chain';
import { CHANNEL_STATUS, GATEWAY_AGENT_ID } from '@/lib/constants';
import { formatAmount, formatDuration } from '@/lib/format';
import { Badge, Button, CopyText, Input, KeyValue, Notice, Panel, TxStatus } from './ui';
import Link from 'next/link';

const STATUS = {
  [CHANNEL_STATUS.Open]: { label: 'Open', tone: 'good' },
  [CHANNEL_STATUS.Closing]: { label: 'Closing', tone: 'warn' },
  [CHANNEL_STATUS.Closed]: { label: 'Closed', tone: 'neutral' },
} as const;

export function PaymentChannelCard({ channel }: { channel: ChannelView }) {
  const wallet = useWalletReady();
  const actions = useChannelActions(wallet.address);
  const now = useNow(1000);
  const [topUpEth, setTopUpEth] = useState('0.001');
  const [confirmClose, setConfirmClose] = useState(false);

  const isGatewayChannel = channel.agentId === GATEWAY_AGENT_ID;
  const ledger = useLedger(channel.id, isGatewayChannel && channel.status === CHANNEL_STATUS.Open);
  const status = STATUS[channel.status as keyof typeof STATUS] ?? STATUS[CHANNEL_STATUS.Closed];

  const secondsLeft = Number(channel.closeDeadline) - Math.floor(now / 1000);
  const windowOver = channel.status === CHANNEL_STATUS.Closing && secondsLeft <= 0;

  let topUpWei: bigint | null = null;
  try {
    topUpWei = parseEther(topUpEth || '0');
  } catch {
    topUpWei = null;
  }
  const spent = ledger.data ? BigInt(ledger.data.lastCumulativeAmount) : null;

  return (
    <Panel>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-volt-400" aria-hidden />
          <CopyText value={channel.id} className="text-sm" />
        </div>
        <div className="flex items-center gap-2">
          <Link href={`/agents/${channel.agentId}`} className="text-xs text-volt-400 hover:underline">
            Agent #{channel.agentId.toString()}
          </Link>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>
      </div>

      <dl>
        <KeyValue label="Deposit">
          <span className="font-mono">{formatAmount(channel.deposit)}</span>
        </KeyValue>
        {spent !== null && (
          <KeyValue label="Used so far (gateway ledger)">
            <span className="font-mono">{formatAmount(spent)}</span>
            <span className="text-slate-500"> · {ledger.data?.lastNonce} calls</span>
          </KeyValue>
        )}
        {channel.claimed > 0n && (
          <KeyValue label="Claimed by agent">
            <span className="font-mono">{formatAmount(channel.claimed)}</span>
          </KeyValue>
        )}
        <KeyValue label="Challenge window">{formatDuration(Number(channel.challengePeriod))}</KeyValue>
        {channel.status === CHANNEL_STATUS.Closing && (
          <KeyValue label="Window ends">{windowOver ? 'Ended: ready to finalize' : `in ${formatDuration(secondsLeft)}`}</KeyValue>
        )}
      </dl>

      <div className="mt-4 space-y-3">
        {channel.status === CHANNEL_STATUS.Open && (
          <>
            <div className="flex gap-2">
              <Input
                aria-label="Top-up amount in ETH"
                inputMode="decimal"
                value={topUpEth}
                onChange={(e) => setTopUpEth(e.target.value)}
                className="max-w-32 font-mono"
              />
              <Button variant="secondary" onClick={() => topUpWei && actions.topUp(channel.id, topUpWei)} disabled={!wallet.ready || !topUpWei || topUpWei <= 0n} loading={actions.busy}>
                Top up (ETH)
              </Button>
            </div>
            {!confirmClose ? (
              <Button variant="ghost" size="sm" onClick={() => setConfirmClose(true)} disabled={!wallet.ready}>
                Close channel…
              </Button>
            ) : (
              <Notice tone="warn">
                <p>
                  Starting a close opens a {formatDuration(Number(channel.challengePeriod))} window. The agent can submit its latest voucher during that time; you get the rest back
                  afterwards. New calls will be refused.
                </p>
                <div className="mt-3 flex gap-2">
                  <Button
                    variant="danger"
                    size="sm"
                    loading={actions.busy}
                    onClick={async () => {
                      await actions.startClose(channel.id);
                      setConfirmClose(false);
                    }}
                  >
                    Start close
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setConfirmClose(false)}>
                    Keep open
                  </Button>
                </div>
              </Notice>
            )}
          </>
        )}

        {channel.status === CHANNEL_STATUS.Closing && (
          <Button onClick={() => actions.finalizeClose(channel.id)} disabled={!wallet.ready || !windowOver} loading={actions.busy}>
            Finalize close
          </Button>
        )}

        {channel.status === CHANNEL_STATUS.Closed && <p className="text-sm text-slate-400">Settled. Refunds and payouts are in the withdrawable balance above.</p>}

        <TxStatus state={actions.state} />
      </div>
    </Panel>
  );
}