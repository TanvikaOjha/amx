'use client';

import { useEffect, useState } from 'react';

const STEPS = ['Task sent', '402 quote', 'Voucher signed', 'Ticket accepted', 'Task ran', 'Sealed on Hedera'];
const randomHash = () => '0x' + Array.from(crypto.getRandomValues(new Uint8Array(32)), (b) => b.toString(16).padStart(2, '0')).join('');
const short = (h: string) => `${h.slice(0, 8)}…${h.slice(-4)}`;

/** Decorative receipt that replays one paid call forever. Starts in its finished state so server and client HTML match. */
export function ReceiptLoop() {
  const [s, setS] = useState({ i: STEPS.length, n: 7, hash: '0x9f3ac41e' + '0'.repeat(52) + 'c41e' });

  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const t = setInterval(
      () => setS((p) => (p.i < STEPS.length ? { ...p, i: p.i + 1 } : { i: 0, n: p.n + 1, hash: randomHash() })),
      750,
    );
    return () => clearInterval(t);
  }, []);

  const done = s.i >= STEPS.length;
  return (
    <div aria-hidden className="relative mx-auto w-full max-w-md rotate-1 border border-slate-700 bg-ink-900 p-6 pb-8 font-mono text-xs shadow-2xl shadow-black/50">
      <div className="mb-3 flex items-baseline justify-between border-b border-dashed border-slate-700 pb-3 text-slate-400">
        <span>voucher</span>
        <b className="text-slate-100">#{s.n}</b>
      </div>
      <ol className="mb-4 flex flex-wrap gap-1.5">
        {STEPS.map((name, i) => (
          <li
            key={name}
            className={`rounded-full border px-2.5 py-0.5 transition-colors ${
              i < s.i ? 'border-volt-400 bg-volt-400 text-ink-950' : i === s.i ? 'border-amber-400 text-amber-300' : 'border-slate-700 text-slate-500'
            }`}
          >
            {name}
          </li>
        ))}
      </ol>
      <dl className="space-y-1.5">
        {[
          ['agent', '#1'],
          ['this call', '1,000 wei'],
          ['channel total', `${(s.n * 1000).toLocaleString('en-US')} wei`],
          ['request', short(s.hash)],
          ['hedera', done ? `sequence #${311 + s.n}` : 'waiting…'],
        ].map(([k, v]) => (
          <div key={k} className="flex justify-between">
            <dt className="text-slate-500">{k}</dt>
            <dd className="text-slate-100">{v}</dd>
          </div>
        ))}
      </dl>
      {/* perforated edge */}
      <div
        className="absolute inset-x-0 -bottom-2 h-4"
        style={{ background: 'radial-gradient(circle at 8px 8px, var(--color-ink-950) 6px, transparent 6.5px) 0 0 / 16px 16px repeat-x' }}
      />
    </div>
  );
}