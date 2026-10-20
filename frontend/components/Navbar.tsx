'use client';

import { ConnectButton } from '@rainbow-me/rainbowkit';
import { Menu, X } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState } from 'react';
import { useWalletReady } from '@/hooks/useWalletReady';
import { CONTRACTS_CONFIGURED, TARGET } from '@/lib/constants';
import { cn } from '@/lib/utils';
import { Button } from './ui';

const LINKS = [
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/agents', label: 'Agents' },
  { href: '/channels', label: 'Channels' },
  { href: '/audit', label: 'Audit log' },
];

export function Navbar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const { wrongNetwork, switchToTarget, switching } = useWalletReady();

  return (
    <header className="sticky top-0 z-40 border-b border-slate-800 bg-ink-950/85 backdrop-blur">
      {wrongNetwork && (
        <div className="flex flex-wrap items-center justify-center gap-3 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">
          Your wallet is on the wrong network. This app uses {TARGET.label}.
          <Button size="sm" variant="secondary" onClick={switchToTarget} loading={switching}>
            Switch to {TARGET.label}
          </Button>
        </div>
      )}
      {!CONTRACTS_CONFIGURED && (
        <div className="bg-rose-500/10 px-4 py-2 text-center text-sm text-rose-200">
          Contract addresses are not configured. Set NEXT_PUBLIC_IDENTITY_ADDRESS and NEXT_PUBLIC_PAYMENT_CHANNEL_ADDRESS in .env.local.
        </div>
      )}
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-4 sm:px-6">
        <div className="flex items-center gap-8">
          <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight text-white">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-volt-400 font-mono text-sm font-bold text-ink-950">X</span>
            Agent-Mesh-X
          </Link>
          <nav className="hidden items-center gap-1 md:flex" aria-label="Main">
            {LINKS.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                aria-current={pathname.startsWith(l.href) ? 'page' : undefined}
                className={cn(
                  'rounded-md px-3 py-1.5 text-sm transition-colors',
                  pathname.startsWith(l.href) ? 'bg-ink-800 text-white' : 'text-slate-400 hover:text-white',
                )}
              >
                {l.label}
              </Link>
            ))}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          <ConnectButton showBalance={false} chainStatus="icon" accountStatus={{ smallScreen: 'avatar', largeScreen: 'full' }} />
          <button
            className="rounded-md p-2 text-slate-300 hover:bg-ink-800 md:hidden"
            onClick={() => setOpen((v) => !v)}
            aria-label={open ? 'Close menu' : 'Open menu'}
            aria-expanded={open}
          >
            {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
        </div>
      </div>
      {open && (
        <nav className="border-t border-slate-800 px-4 py-2 md:hidden" aria-label="Mobile">
          {LINKS.map((l) => (
            <Link key={l.href} href={l.href} onClick={() => setOpen(false)} className="block rounded-md px-3 py-2.5 text-sm text-slate-200 hover:bg-ink-800">
              {l.label}
            </Link>
          ))}
        </nav>
      )}
    </header>
  );
}