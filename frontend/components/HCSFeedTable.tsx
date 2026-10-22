'use client';

import { CheckCircle2, ChevronDown, ChevronRight, CircleHelp, ShieldAlert } from 'lucide-react';
import { Fragment, useState } from 'react';
import type { FeedRow, Verification } from '@/hooks/useHcsFeed';
import { shortHex } from '@/lib/format';
import { Badge, CopyText, EmptyState } from './ui';

function VerificationBadge({ v }: { v: Verification }) {
  switch (v.state) {
    case 'valid':
      return (
        <Badge tone="good">
          <CheckCircle2 className="h-3 w-3" aria-hidden /> Signed by agent
        </Badge>
      );
    case 'wrong_signer':
      return (
        <Badge tone="bad">
          <ShieldAlert className="h-3 w-3" aria-hidden /> Wrong signer
        </Badge>
      );
    case 'bad_signature':
      return (
        <Badge tone="bad">
          <ShieldAlert className="h-3 w-3" aria-hidden /> Bad signature
        </Badge>
      );
    default:
      return (
        <Badge>
          <CircleHelp className="h-3 w-3" aria-hidden /> Not an AMX proof
        </Badge>
      );
  }
}

/**
 * Each row is one gateway proof. "Signed by agent" means the signature recovers to ERC8004Identity.signerOf(agentId),
 * checked in this browser: the mirror node is not trusted.
 */
export function HCSFeedTable({ rows }: { rows: FeedRow[] }) {
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  if (rows.length === 0) return <EmptyState title="No messages yet">Run a task from an agent page. Its proof appears here a few seconds after consensus.</EmptyState>;

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-800 bg-ink-900/70">
      <table className="w-full min-w-[56rem] text-left text-sm">
        <thead className="border-b border-slate-800 text-xs text-slate-400">
          <tr>
            <th className="w-8 p-3" />
            <th className="p-3 font-medium">Sequence</th>
            <th className="p-3 font-medium">Consensus time</th>
            <th className="p-3 font-medium">Agent</th>
            <th className="p-3 font-medium">Channel · nonce</th>
            <th className="p-3 font-medium">Request hash</th>
            <th className="p-3 font-medium">Verification</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/80">
          {rows.map((row) => {
            const isOpen = openSeq === row.sequence;
            return (
              <Fragment key={row.sequence}>
                <tr className="hover:bg-ink-800/40">
                  <td className="p-3">
                    <button
                      aria-label={isOpen ? 'Hide details' : 'Show details'}
                      aria-expanded={isOpen}
                      onClick={() => setOpenSeq(isOpen ? null : row.sequence)}
                      className="rounded p-0.5 text-slate-400 hover:text-white"
                    >
                      {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    </button>
                  </td>
                  <td className="p-3 font-mono font-semibold tabular-nums text-volt-300">#{row.sequence}</td>
                  <td className="p-3 text-slate-300">{row.consensus.toLocaleString()}</td>
                  <td className="p-3 font-mono text-xs">{row.proof ? `#${row.proof.agentId}` : '—'}</td>
                  <td className="p-3 font-mono text-xs">{row.proof ? `${shortHex(row.proof.channelId, 6, 4)} · ${row.proof.nonce}` : '—'}</td>
                  <td className="p-3">{row.proof ? <CopyText value={row.proof.requestHash} /> : '—'}</td>
                  <td className="p-3">
                    <VerificationBadge v={row.verification} />
                  </td>
                </tr>
                {isOpen && (
                  <tr className="bg-ink-950/60">
                    <td />
                    <td colSpan={6} className="p-4">
                      <div className="grid gap-4 text-xs md:grid-cols-2">
                        <dl className="space-y-1.5 text-slate-400">
                          <div>Message size: <span className="font-mono text-slate-200">{row.sizeBytes} / 1024 bytes</span></div>
                          {row.payer && <div>Submitted by Hedera account: <span className="font-mono text-slate-200">{row.payer}</span></div>}
                          {row.proof && <div>Proof timestamp: <span className="font-mono text-slate-200">{new Date(row.proof.ts * 1000).toLocaleString()}</span></div>}
                          {row.verification.state === 'wrong_signer' && (
                            <div className="text-rose-300">
                              Recovered {row.verification.signer} but the registry says {row.verification.expected ?? 'unknown'}.
                            </div>
                          )}
                        </dl>
                        <pre className="max-h-56 overflow-auto rounded-lg border border-slate-800 bg-ink-950 p-3 font-mono text-[11px] leading-relaxed text-volt-200">
                          {row.proof ? JSON.stringify(row.proof, null, 2) : 'This message is not an AMX proof.'}
                        </pre>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
