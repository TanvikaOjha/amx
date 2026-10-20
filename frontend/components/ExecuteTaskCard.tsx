'use client';

import { useQueryClient } from '@tanstack/react-query';
import { ExternalLink, Zap } from 'lucide-react';
import Link from 'next/link';
import { useRef, useState } from 'react';
import type { Hex } from 'viem';
import { useMyChannels } from '@/hooks/usePaymentChannel';
import { useSignVoucher } from '@/hooks/useSignVoucher';
import { useWalletReady } from '@/hooks/useWalletReady';
import { canonicalJson, requestHash } from '@/lib/canonical';
import { CHANNEL_HINT_HEADER, CHANNEL_STATUS, TASKS, type TaskName } from '@/lib/constants';
import { encodeTicket, toWireVoucher, type VoucherStruct } from '@/lib/eip712';
import { formatAmount } from '@/lib/format';
import { AmxError, describeReason, planVoucher, postExecute, selectAccept, submitPaid, type GatewayResult, type Plan } from '@/lib/gateway';
import { hashscanTopicUrl } from '@/lib/hedera';
import { pushHistory } from '@/lib/history';
import { loadSigned, saveSigned, type SignedState } from '@/lib/signedState';
import { Badge, Button, Field, KeyValue, Notice, Panel, PanelTitle, Select, Textarea } from './ui';

interface Unsettled {
  ticket: string;
  bodyText: string;
  requestHash: Hex;
  channelId: Hex;
  nonce: bigint;
  cumulative: bigint;
  previous: SignedState | null;
  reused: boolean;
}

interface Quote {
  channelId: Hex;
  bodyText: string;
  requestHash: Hex;
  plan: Plan;
  resend: Unsettled | null;
}

const TASK_HELP: Record<TaskName, string> = {
  echo: 'Returns your input unchanged.',
  sha256: 'Returns the SHA-256 hex digest of your input.',
  word_count: 'Returns how many words your input has.',
};

/**
 * Probe (402) → review quote → sign voucher in wallet → single paid attempt.
 * Never loops. If the outcome is unknown, the SAME ticket is re-sent next time, which the gateway serves
 * idempotently without a second charge.
 */
export function ExecuteTaskCard({ agentId }: { agentId: bigint }) {
  const wallet = useWalletReady();
  const { signVoucher } = useSignVoucher();
  const queryClient = useQueryClient();
  const channels = useMyChannels(wallet.address);
  const usable = (channels.data ?? []).filter((c) => c.status === CHANNEL_STATUS.Open && c.agentId === agentId);

  const [channelId, setChannelId] = useState<Hex | ''>('');
  const [task, setTask] = useState<TaskName>('echo');
  const [input, setInput] = useState('hello mesh');
  const [busy, setBusy] = useState<'quote' | 'pay' | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [ack, setAck] = useState(false);
  const [result, setResult] = useState<{ data: GatewayResult; pending: Unsettled } | null>(null);
  const [problem, setProblem] = useState<{ tone: 'bad' | 'warn'; text: string } | null>(null);
  const unsettled = useRef(new Map<string, Unsettled>());

  const channel = usable.find((c) => c.id === channelId) ?? usable[0];

  const fail = (text: string, tone: 'bad' | 'warn' = 'bad') => setProblem({ tone, text });

  async function getQuote() {
    if (!channel) return;
    setProblem(null);
    setResult(null);
    setQuote(null);
    setAck(false);
    setBusy('quote');
    try {
      const body = { task, input };
      let bodyText: string;
      let hash: Hex;
      try {
        bodyText = canonicalJson(body);
        hash = requestHash(body);
      } catch (e) {
        throw new AmxError('invalid_input', e instanceof Error ? e.message : 'The input cannot be encoded.');
      }
      if (new TextEncoder().encode(bodyText).length > 16_384) throw new AmxError('invalid_input', 'The request is larger than the gateway’s 16 KB limit.');

      const earlier = unsettled.current.get(`${channel.id}:${hash}`);
      if (earlier) {
        const local = loadSigned(channel.id);
        setQuote({
          channelId: channel.id,
          bodyText,
          requestHash: hash,
          resend: earlier,
          plan: { nonce: earlier.nonce, cumulative: earlier.cumulative, price: 0n, gatewayPrevious: local?.cumulative ?? 0n, gatewayAhead: false },
        });
        return;
      }

      const reply = await postExecute(bodyText, { [CHANNEL_HINT_HEADER]: channel.id });
      if (reply.status === 429) throw new AmxError('rate_limited', describeReason('rate_limited'));
      if (reply.status !== 402) throw new AmxError('unexpected_probe_status', `Expected a payment challenge (HTTP 402) from the gateway but got HTTP ${reply.status}. Is the gateway running?`);

      const accept = selectAccept(reply.json);
      const plan = planVoucher(accept, loadSigned(channel.id));
      if (plan.cumulative > channel.deposit) {
        throw new AmxError('exceeds_deposit', `This call would bring the total owed to ${formatAmount(plan.cumulative)}, above the channel deposit of ${formatAmount(channel.deposit)}. Top the channel up first.`);
      }
      setQuote({ channelId: channel.id, bodyText, requestHash: hash, plan, resend: null });
    } catch (e) {
      fail(e instanceof AmxError ? e.message : 'Could not reach the gateway. Is it running, and is GATEWAY_URL correct?');
    } finally {
      setBusy(null);
    }
  }

  async function pay() {
    if (!quote || !wallet.address) return;
    setProblem(null);
    setBusy('pay');
    let pending: Unsettled;
    try {
      if (quote.resend) {
        pending = { ...quote.resend, reused: true };
      } else {
        const voucher: VoucherStruct = {
          channelId: quote.channelId,
          agentId,
          cumulativeAmount: quote.plan.cumulative,
          nonce: quote.plan.nonce,
          expiry: BigInt(Math.floor(Date.now() / 1000) + 300),
          requestHash: quote.requestHash,
        };
        const signature = await signVoucher(voucher);
        const previous = loadSigned(quote.channelId);
        pending = {
          ticket: encodeTicket(toWireVoucher(voucher), signature),
          bodyText: quote.bodyText,
          requestHash: quote.requestHash,
          channelId: quote.channelId,
          nonce: quote.plan.nonce,
          cumulative: quote.plan.cumulative,
          previous,
          reused: false,
        };
        // Persist BEFORE the ticket leaves the browser, like the MCP signer: a crash must never allow a nonce to be reused.
        saveSigned(pending.channelId, { nonce: pending.nonce, cumulative: pending.cumulative });
      }
    } catch (e) {
      setBusy(null);
      fail(e instanceof Error && /reject|denied/i.test(e.message) ? 'You rejected the signature request. Nothing was sent or charged.' : e instanceof Error ? e.message : 'Signing failed.');
      return;
    }

    const key = `${pending.channelId}:${pending.requestHash}`;
    const outcome = await submitPaid(pending.bodyText, pending.ticket);
    setBusy(null);
    setQuote(null);

    switch (outcome.kind) {
      case 'ok': {
        const r = outcome.result;
        if (r.request_hash !== pending.requestHash || r.channel_id !== pending.channelId || BigInt(r.nonce) !== pending.nonce || r.cumulative_amount !== pending.cumulative.toString()) {
          unsettled.current.delete(key);
          fail('The gateway’s receipt does not match the voucher you signed, so the result is not trusted. You may have been charged.');
          return;
        }
        unsettled.current.delete(key);
        setResult({ data: r, pending });
        pushHistory({
          jobId: r.job_id,
          task: r.task,
          agentId: Number(agentId),
          channelId: r.channel_id,
          nonce: r.nonce,
          charged: r.charged_amount,
          cumulative: r.cumulative_amount,
          hcsStatus: r.hcs_status,
          hcsTopicId: r.hcs_topic_id,
          hcsSequence: r.hcs_sequence_number,
          replay: r.idempotent_replay,
          at: Date.now(),
        });
        void queryClient.invalidateQueries({ queryKey: ['amx'] });
        return;
      }
      case 'rejected':
        unsettled.current.delete(key);
        if (!pending.reused) saveSigned(pending.channelId, pending.previous); // definitively not charged: free the nonce
        fail(`Payment rejected: ${describeReason(outcome.code)} You were not charged.`);
        return;
      case 'in_progress':
        unsettled.current.set(key, pending);
        fail('The gateway is still processing this exact request. Press “Get quote” again in a moment to re-send the same ticket.', 'warn');
        return;
      case 'malformed':
        unsettled.current.delete(key);
        fail('The gateway answered 200 but the result was unreadable. You were charged.');
        return;
      default:
        unsettled.current.set(key, pending);
        fail('The connection failed after the ticket was sent, so it is unknown whether the task ran or you were charged. Run the same task again: the same ticket is re-sent and cannot be charged twice.', 'warn');
    }
  }

  /** Re-send the settled ticket: the gateway replays the stored response with the latest HCS status, at no charge. */
  async function refreshAudit() {
    if (!result) return;
    setBusy('pay');
    const outcome = await submitPaid(result.pending.bodyText, result.pending.ticket);
    setBusy(null);
    if (outcome.kind === 'ok' && outcome.result.job_id === result.data.job_id) setResult({ ...result, data: outcome.result });
  }

  const noChannel = wallet.isConnected && !channels.isLoading && usable.length === 0;

  return (
    <Panel>
      <PanelTitle icon={<Zap className="h-5 w-5" aria-hidden />}>Run a task</PanelTitle>

      {!wallet.isConnected && <Notice tone="warn">Connect the wallet that opened your payment channel.</Notice>}
      {wallet.wrongNetwork && <Notice tone="warn">Switch your wallet to the app’s network to sign vouchers.</Notice>}
      {noChannel && (
        <Notice tone="warn">
          You have no open channel for agent #{agentId.toString()}.{' '}
          <Link href="/channels" className="font-semibold underline">
            Open one
          </Link>{' '}
          to pay for calls.
        </Notice>
      )}

      <div className="mt-4 space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Task" hint={TASK_HELP[task]}>
            <Select value={task} onChange={(e) => setTask(e.target.value as TaskName)}>
              {TASKS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Pay from channel">
            <Select value={channel?.id ?? ''} onChange={(e) => setChannelId(e.target.value as Hex)} disabled={usable.length === 0}>
              {usable.length === 0 && <option value="">No open channel</option>}
              {usable.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id.slice(0, 10)}… · {formatAmount(c.deposit)} deposit
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Input" hint="Up to 8,192 characters. Only a hash of it goes on Hedera.">
          <Textarea value={input} onChange={(e) => setInput(e.target.value)} maxLength={8192} />
        </Field>

        {!quote && (
          <Button onClick={getQuote} loading={busy === 'quote'} disabled={!wallet.ready || !channel}>
            Get quote
          </Button>
        )}

        {quote && (
          <div className="rounded-lg border border-volt-500/30 bg-volt-500/5 p-4">
            <p className="mb-2 text-sm font-semibold text-slate-100">{quote.resend ? 'Re-send your earlier signed ticket' : 'Review before signing'}</p>
            <dl>
              {!quote.resend && (
                <KeyValue label="Price for this call">
                  <span className="font-mono">{formatAmount(quote.plan.price)}</span>
                </KeyValue>
              )}
              <KeyValue label="Voucher total (cumulative)">
                <span className="font-mono">{formatAmount(quote.plan.cumulative)}</span>
              </KeyValue>
              <KeyValue label="Voucher number">
                <span className="font-mono">{quote.plan.nonce.toString()}</span>
              </KeyValue>
              <KeyValue label="Request hash">
                <span className="font-mono text-xs">{quote.requestHash.slice(0, 18)}…</span>
              </KeyValue>
            </dl>
            {quote.resend ? (
              <p className="mt-3 text-xs text-slate-400">No new signature is needed. The gateway returns the original result without charging again.</p>
            ) : (
              <p className="mt-3 text-xs text-slate-400">Signing authorizes the agent to claim up to the voucher total from your channel. It costs no gas.</p>
            )}
            {quote.plan.gatewayAhead && (
              <Notice tone="warn" className="mt-3">
                <p>
                  The gateway says this channel has already been charged {formatAmount(quote.plan.gatewayPrevious)}, more than this browser has signed. That happens if you used the channel
                  from another device, cleared browser data, or the gateway is misreporting.
                </p>
                <label className="mt-2 flex items-center gap-2">
                  <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="accent-cyan-400" />I trust the gateway’s ledger and want to sign anyway
                </label>
              </Notice>
            )}
            <div className="mt-4 flex gap-2">
              <Button onClick={pay} loading={busy === 'pay'} disabled={!wallet.ready || (quote.plan.gatewayAhead && !ack)}>
                {quote.resend ? 'Re-send ticket' : 'Sign and run'}
              </Button>
              <Button variant="ghost" onClick={() => setQuote(null)} disabled={busy === 'pay'}>
                Cancel
              </Button>
            </div>
          </div>
        )}

        {problem && <Notice tone={problem.tone}>{problem.text}</Notice>}
      </div>

      {result && <ResultView result={result.data} onRefresh={refreshAudit} refreshing={busy === 'pay'} />}
    </Panel>
  );
}

function ResultView({ result, onRefresh, refreshing }: { result: GatewayResult; onRefresh: () => void; refreshing: boolean }) {
  const hcsTone = result.hcs_status === 'confirmed' ? 'good' : result.hcs_status === 'pending' ? 'warn' : result.hcs_status === 'failed' ? 'bad' : 'neutral';
  return (
    <div className="mt-6 space-y-4 border-t border-slate-800 pt-5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-semibold text-slate-100">Result</h3>
        {result.idempotent_replay && <Badge tone="info">Replayed, not charged again</Badge>}
      </div>
      {/* The output is data from a remote agent: rendered as plain text, never as markup or instructions. */}
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-ink-950 p-3 font-mono text-sm text-volt-200">{result.output}</pre>

      <dl>
        <KeyValue label="Charged">
          <span className="font-mono">{formatAmount(BigInt(result.charged_amount))}</span>
        </KeyValue>
        <KeyValue label="Voucher">
          <span className="font-mono">
            #{result.nonce} · total {formatAmount(BigInt(result.cumulative_amount))}
          </span>
        </KeyValue>
        <KeyValue label="Audit proof">
          <Badge tone={hcsTone}>{result.hcs_status}</Badge>
          {result.hcs_sequence_number !== null && <span className="ml-2 font-mono">#{result.hcs_sequence_number}</span>}
        </KeyValue>
      </dl>

      {result.hcs_status === 'pending' && (
        <Notice>
          The result is yours; the audit proof is queued and will land on Hedera shortly.{' '}
          <button onClick={onRefresh} disabled={refreshing} className="font-semibold underline disabled:opacity-50">
            Check again
          </button>
        </Notice>
      )}
      {result.hcs_status === 'disabled' && <Notice>This gateway has Hedera audit logging switched off.</Notice>}
      {result.hcs_topic_id && (
        <div className="flex flex-wrap gap-4 text-sm">
          <a className="inline-flex items-center gap-1 text-volt-400 hover:underline" href={hashscanTopicUrl(result.hcs_topic_id)} target="_blank" rel="noopener noreferrer">
            View topic on HashScan <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </a>
          <Link className="text-volt-400 hover:underline" href="/audit">
            Verify in the audit log
          </Link>
        </div>
      )}
    </div>
  );
}