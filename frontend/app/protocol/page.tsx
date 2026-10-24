'use client';

import { Badge, KeyValue, Panel, PanelTitle } from '@/components/ui';
import { formatAmount, shortHex } from '@/lib/format';
import { useExecutionHistory } from '@/lib/history';

const FIELDS: [string, string][] = [
  ['channelId', 'which channel is paying'],
  ['agentId', 'the ERC-8004 token being paid'],
  ['cumulativeAmount', 'total owed so far; only ever grows'],
  ['nonce', 'strictly increasing per channel'],
  ['expiry', 'unix seconds, checked by the gateway'],
  ['requestHash', 'sha256 of the canonical request, binding one voucher to one job'],
];

const RULES = [
  'A nonce must be higher than the last accepted one on that channel.',
  'The total must rise by at least the price of one call.',
  'Expired vouchers and mismatched request hashes are refused.',
  'The total can never exceed the channel deposit.',
  'The same ticket with the same body returns the original result and charges nothing.',
  'Signatures must be 65 bytes, low-s, v of 27 or 28: exactly what the contract accepts.',
];

const QUOTE = `{
  "error": "payment_required",
  "accepts": [{
    "scheme": "amx-voucher",
    "network": "sepolia",
    "channelContract": "0x…",
    "agentId": 1,
    "pricePerCall": "1000",
    "asset": "native",
    "minNonce": 7,
    "minCumulativeAmount": "7000"
  }]
}`;

const PROOF = `{ agentId, channelId, nonce, requestHash,
  responseHash, ts, gatewaySig }`;

export default function ProtocolPage() {
  const history = useExecutionHistory();
  const last = history[0];

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight text-white">Protocol</h1>
        <p className="mt-1 text-slate-400">Four small rules, shared by the contract, the gateway, the MCP server and this app.</p>
      </div>

      <Panel>
        <PanelTitle>The voucher</PanelTitle>
        <p className="mb-2 text-sm text-slate-400">An EIP-712 message signed by the payer. The gateway and the contract verify the same bytes.</p>
        <dl>
          {FIELDS.map(([k, v]) => (
            <KeyValue key={k} label={k}>
              <span className="text-sm text-slate-300">{v}</span>
            </KeyValue>
          ))}
        </dl>
      </Panel>

      <Panel>
        <PanelTitle>The 402 quote</PanelTitle>
        <p className="mb-3 text-sm text-slate-400">Sent when a call arrives without a ticket. The client checks it against local limits before signing anything.</p>
        <pre className="overflow-x-auto rounded-lg border border-slate-800 bg-ink-950 p-3 font-mono text-xs text-volt-200">{QUOTE}</pre>
      </Panel>

      <Panel>
        <PanelTitle>Rules the gateway enforces</PanelTitle>
        <ul className="list-disc space-y-1.5 pl-5 text-sm text-slate-300">
          {RULES.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </Panel>

      <Panel>
        <PanelTitle aside={last ? <Badge tone="info">from this browser</Badge> : undefined}>Your latest call</PanelTitle>
        {last ? (
          <dl>
            <KeyValue label="Task"><span className="font-mono text-sm">{last.task}</span></KeyValue>
            <KeyValue label="Channel"><span className="font-mono text-xs">{shortHex(last.channelId, 8, 6)}</span></KeyValue>
            <KeyValue label="Voucher number"><span className="font-mono">{last.nonce}</span></KeyValue>
            <KeyValue label="Cumulative"><span className="font-mono">{formatAmount(BigInt(last.cumulative))}</span></KeyValue>
            <KeyValue label="Charged"><span className="font-mono">{formatAmount(BigInt(last.charged))}</span></KeyValue>
          </dl>
        ) : (
          <p className="text-sm text-slate-400">Run a task from an agent page and its voucher details appear here.</p>
        )}
      </Panel>

      <Panel>
        <PanelTitle>The Hedera proof</PanelTitle>
        <p className="mb-3 text-sm text-slate-400">At most 1,024 bytes. Hashes only, signed by the agent’s registered key. Task content never goes on-chain.</p>
        <pre className="overflow-x-auto rounded-lg border border-slate-800 bg-ink-950 p-3 font-mono text-xs text-volt-200">{PROOF}</pre>
      </Panel>
    </div>
  );
}