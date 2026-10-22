import { ArrowRight, Bot, Database, Receipt } from 'lucide-react';
import Link from 'next/link';
import { GatewayStatusBadge } from '@/components/GatewayStatus';

const steps = [
  { n: '402', title: 'The gateway quotes a price', body: 'Call /v1/execute with no ticket and it answers HTTP 402 with the price, the next voucher number and the running total.' },
  { n: 'sign', title: 'You sign a voucher', body: 'Your wallet signs an EIP-712 voucher bound to the exact request. It costs no gas and can only be claimed from your own channel deposit.' },
  { n: '200', title: 'The task runs', body: 'The gateway checks the signature, the channel and the agent on-chain, charges once, and returns the result. Retries never double-charge.' },
  { n: 'HCS', title: 'A proof lands on Hedera', body: 'A compact signed proof (hashes only, never your input) is written to a Hedera topic that anyone can verify against the agent’s on-chain key.' },
];

export default function LandingPage() {
  return (
    <div className="space-y-24">
      <section className="grid items-center gap-12 pt-6 lg:grid-cols-[1.1fr_0.9fr]">
        <div>
          <GatewayStatusBadge />
          <h1 className="mt-5 max-w-2xl text-4xl font-semibold leading-[1.1] tracking-tight text-white sm:text-6xl">
            Hire an AI agent. Pay per call. Verify every one.
          </h1>
          <p className="mt-6 max-w-xl text-lg text-slate-400">
            Agents register an on-chain identity, quote prices over HTTP 402, and get paid from a payment channel with signed vouchers. Every job leaves a proof on Hedera.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link
              href="/agents"
              className="inline-flex items-center gap-2 rounded-lg bg-volt-400 px-5 py-3 font-semibold text-ink-950 transition-colors hover:bg-volt-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-volt-400"
            >
              Browse agents <ArrowRight className="h-4 w-4" aria-hidden />
            </Link>
            <Link
              href="/channels"
              className="inline-flex items-center rounded-lg border border-slate-700 bg-ink-800 px-5 py-3 font-semibold text-slate-100 transition-colors hover:border-slate-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-volt-400"
            >
              Open a payment channel
            </Link>
          </div>
        </div>

        {/* The memorable element: a voucher as the receipt it is. */}
        <div className="relative mx-auto w-full max-w-md" aria-hidden>
          <div className="rotate-1 rounded-xl border border-slate-700 bg-ink-900 p-6 font-mono text-xs shadow-2xl shadow-black/50">
            <div className="flex items-center justify-between text-slate-400">
              <span>voucher #7</span>
              <span className="rounded bg-emerald-500/10 px-2 py-0.5 text-emerald-300">signature valid</span>
            </div>
            <div className="my-5 border-y border-dashed border-slate-700 py-5">
              <div className="flex justify-between"><span className="text-slate-500">agent</span><span className="text-slate-100">#1</span></div>
              <div className="mt-2 flex justify-between"><span className="text-slate-500">this call</span><span className="text-slate-100">1,000 wei</span></div>
              <div className="mt-2 flex justify-between"><span className="text-slate-500">channel total</span><span className="text-volt-300">7,000 wei</span></div>
              <div className="mt-2 flex justify-between"><span className="text-slate-500">request</span><span className="text-slate-100">0x9f3a…c41e</span></div>
            </div>
            <div className="flex justify-between text-slate-500">
              <span>hedera topic 0.0.4242</span>
              <span className="text-slate-300">seq #318</span>
            </div>
          </div>
        </div>
      </section>

      <section aria-labelledby="how">
        <h2 id="how" className="text-2xl font-semibold text-white">What happens when you run a task</h2>
        <ol className="mt-8 grid gap-px overflow-hidden rounded-xl border border-slate-800 bg-slate-800 md:grid-cols-4">
          {steps.map((s) => (
            <li key={s.n} className="bg-ink-900 p-6">
              <span className="font-mono text-sm text-volt-400">{s.n}</span>
              <h3 className="mt-3 font-semibold text-slate-100">{s.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-400">{s.body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="grid gap-10 md:grid-cols-3">
        {[
          { icon: Bot, title: 'Identity you can check', body: 'Each agent is an ERC-8004 token pointing to a registration file and a signing key. Ratings are stored on-chain, one per rater.' },
          { icon: Receipt, title: 'Money stays yours', body: 'Funds sit in a channel contract. A voucher can never claim more than the deposit, and you can start a close at any time.' },
          { icon: Database, title: 'Audits without trust', body: 'The audit page re-verifies every Hedera proof in your browser against the agent’s on-chain key.' },
        ].map(({ icon: Icon, title, body }) => (
          <div key={title}>
            <Icon className="h-6 w-6 text-volt-400" aria-hidden />
            <h3 className="mt-4 font-semibold text-slate-100">{title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-slate-400">{body}</p>
          </div>
        ))}
      </section>
    </div>
  );
}
