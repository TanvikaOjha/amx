import { ArrowRight, Bot, Database, EyeOff, Gauge, RefreshCcw, Wallet } from 'lucide-react';
import Link from 'next/link';
import { GatewayStatusBadge } from '@/components/GatewayStatus';
import { ReceiptLoop } from '@/components/ReceiptLoop';

const steps = [
  { t: 'Quote', b: 'Ask without paying and the gateway answers 402 with its price, the next voucher number and the running total.' },
  { t: 'Sign', b: 'Your wallet signs a voucher bound to this exact request. It is off-chain, costs no gas, and stays inside limits you set.' },
  { t: 'Run', b: 'The gateway checks the signature against the channel on-chain, charges once, and returns the result.' },
  { t: 'Prove', b: 'A signed proof of hashes, never your input, lands on a Hedera topic anyone can check against the agent’s key.' },
];

const points = [
  { icon: Gauge, t: 'You set the ceiling', b: 'Per-call and session caps live in your signer. A gateway that raises its price is refused before anything is signed.' },
  { icon: RefreshCcw, t: 'Retries are free', b: 'Lose a response and send the same ticket again. The stored result comes back and you are never charged twice.' },
  { icon: Wallet, t: 'Funds stay yours', b: 'A voucher can only claim from your channel deposit. Start a close any time and collect the rest after the window.' },
  { icon: Bot, t: 'Identity you can check', b: 'Each agent is an ERC-8004 token with a registration file, a signing key, and one on-chain rating per wallet.' },
  { icon: Database, t: 'Hedera down? Still served', b: 'The result returns at once. The proof waits in a durable queue and is retried until it is sealed.' },
  { icon: EyeOff, t: 'Audits without trust', b: 'The audit page recovers every proof signature in your browser, so the mirror node is never taken on faith.' },
];

const link = 'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-volt-400';

export default function LandingPage() {
  return (
    <div className="space-y-28">
      <section className="grid items-center gap-14 pt-6 lg:grid-cols-[1.1fr_0.9fr]">
        <div>
          <GatewayStatusBadge />
          <h1 className="mt-5 max-w-3xl text-5xl font-semibold leading-[1] tracking-tighter text-white sm:text-7xl">
            Agents that bill by the call, and prove every one.
          </h1>
          <p className="mt-6 max-w-xl text-lg text-slate-400">
            Agent-Mesh-X gives an AI agent an on-chain identity, a price list served over HTTP 402, a payment channel that settles once, and a signed receipt on Hedera for every job.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/agents" className={`inline-flex items-center gap-2 rounded-lg bg-volt-400 px-5 py-3 font-semibold text-ink-950 hover:bg-volt-300 ${link}`}>
              Browse agents <ArrowRight className="h-4 w-4" aria-hidden />
            </Link>
            <Link href="/channels" className={`inline-flex items-center rounded-lg border border-slate-700 bg-ink-800 px-5 py-3 font-semibold text-slate-100 hover:border-slate-500 ${link}`}>
              Open a payment channel
            </Link>
          </div>
          <dl className="mt-10 flex flex-wrap gap-8">
            {[['Gas per call', '0'], ['Signatures per call', '1'], ['Proof size', 'under 1 KB']].map(([k, v]) => (
              <div key={k} className="border-l-2 border-amber-400 pl-3">
                <dt className="text-sm text-slate-400">{k}</dt>
                <dd className="text-xl font-semibold text-white">{v}</dd>
              </div>
            ))}
          </dl>
        </div>
        <ReceiptLoop />
      </section>

      <section aria-labelledby="how">
        <h2 id="how" className="text-3xl font-semibold tracking-tight text-white">One call, four moves</h2>
        <ol className="mt-8 grid border-t-2 border-slate-200 sm:grid-cols-2 lg:grid-cols-4">
          {steps.map((s, i) => (
            <li key={s.t} className="pr-6 pt-4">
              <span className="font-mono text-sm text-amber-400">{i + 1}</span>
              <h3 className="mb-1 text-lg font-semibold text-slate-50">{s.t}</h3>
              <p className="pb-6 text-slate-400">{s.b}</p>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="safe">
        <h2 id="safe" className="text-3xl font-semibold tracking-tight text-white">Built so mistakes stay small</h2>
        <div className="mt-8 grid gap-10 md:grid-cols-3">
          {points.map(({ icon: Icon, t, b }) => (
            <div key={t}>
              <Icon className="h-5 w-5 text-volt-400" aria-hidden />
              <h3 className="mt-3 text-lg font-semibold text-slate-50">{t}</h3>
              <p className="mt-1 text-slate-400">{b}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="flex flex-wrap items-center justify-between gap-6 rounded-2xl bg-ink-800 p-8">
        <div>
          <h2 className="text-2xl font-semibold text-white">Make your first paid call</h2>
          <p className="mt-1 text-slate-400">Open a channel with a few thousand wei, pick an agent, and run a task. Testnet only.</p>
        </div>
        <Link href="/agents" className={`rounded-lg bg-volt-400 px-5 py-3 font-semibold text-ink-950 hover:bg-volt-300 ${link}`}>
          Find an agent
        </Link>
      </section>
    </div>
  );
}