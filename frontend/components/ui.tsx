'use client';

import { Check, Copy, ExternalLink, Loader2, TriangleAlert, Info, CircleCheck } from 'lucide-react';
import { useEffect, useRef, useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react';
import { explorerUrl, shortHex } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { TxState } from '@/hooks/useTx';

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------

const buttonVariants = {
  primary: 'bg-volt-400 text-ink-950 hover:bg-volt-300 disabled:bg-ink-700 disabled:text-slate-500',
  secondary: 'bg-ink-800 text-slate-100 border border-slate-700 hover:border-slate-500 disabled:text-slate-600',
  ghost: 'text-slate-300 hover:bg-ink-800 hover:text-white disabled:text-slate-600',
  danger: 'bg-rose-500/10 text-rose-300 border border-rose-500/40 hover:bg-rose-500/20 disabled:text-slate-600 disabled:border-slate-700',
} as const;

export function Button({
  variant = 'primary',
  size = 'md',
  loading,
  className,
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof buttonVariants; size?: 'sm' | 'md'; loading?: boolean }) {
  return (
    <button
      {...props}
      disabled={disabled || loading}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-lg font-semibold transition-colors disabled:cursor-not-allowed',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-volt-400',
        size === 'sm' ? 'px-3 py-1.5 text-xs' : 'px-4 py-2.5 text-sm',
        buttonVariants[variant],
        className,
      )}
    >
      {loading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Surfaces
// ---------------------------------------------------------------------------

export function Panel({ className, children, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...props} className={cn('rounded-xl border border-slate-800 bg-ink-900/70 p-5', className)}>
      {children}
    </div>
  );
}

export function PanelTitle({ icon, children, aside }: { icon?: ReactNode; children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-4 flex items-center justify-between gap-3">
      <h2 className="flex items-center gap-2 text-base font-semibold text-slate-100">
        {icon && <span className="text-volt-400">{icon}</span>}
        {children}
      </h2>
      {aside}
    </div>
  );
}

const badgeTones = {
  neutral: 'bg-slate-800 text-slate-300 border-slate-700',
  good: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
  warn: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
  bad: 'bg-rose-500/10 text-rose-300 border-rose-500/30',
  info: 'bg-volt-500/10 text-volt-300 border-volt-500/30',
} as const;

export function Badge({ tone = 'neutral', children, className }: { tone?: keyof typeof badgeTones; children: ReactNode; className?: string }) {
  return <span className={cn('inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium', badgeTones[tone], className)}>{children}</span>;
}

export function Stat({ label, value, hint, icon }: { label: string; value: ReactNode; hint?: ReactNode; icon?: ReactNode }) {
  return (
    <Panel>
      <div className="flex items-center justify-between text-sm text-slate-400">
        <span>{label}</span>
        {icon && <span className="text-volt-400">{icon}</span>}
      </div>
      <div className="mt-3 break-words font-mono text-2xl font-semibold tabular-nums text-slate-50">{value}</div>
      {hint && <div className="mt-2 text-xs text-slate-500">{hint}</div>}
    </Panel>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-700 px-6 py-12 text-center">
      <p className="font-semibold text-slate-200">{title}</p>
      {children && <p className="mx-auto mt-1 max-w-md text-sm text-slate-400">{children}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

const noticeTones = {
  info: { cls: 'border-volt-500/30 bg-volt-500/5 text-volt-200', icon: Info },
  warn: { cls: 'border-amber-500/30 bg-amber-500/5 text-amber-200', icon: TriangleAlert },
  bad: { cls: 'border-rose-500/30 bg-rose-500/5 text-rose-200', icon: TriangleAlert },
  good: { cls: 'border-emerald-500/30 bg-emerald-500/5 text-emerald-200', icon: CircleCheck },
} as const;

export function Notice({ tone = 'info', children, className }: { tone?: keyof typeof noticeTones; children: ReactNode; className?: string }) {
  const { cls, icon: Icon } = noticeTones[tone];
  return (
    <div role={tone === 'bad' ? 'alert' : 'status'} className={cn('flex gap-2.5 rounded-lg border px-3.5 py-3 text-sm', cls, className)}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function TxStatus({ state }: { state: TxState }) {
  if (state.status === 'idle') return null;
  if (state.status === 'signing') return <Notice>Confirm the transaction in your wallet…</Notice>;
  if (state.status === 'confirming')
    return (
      <Notice>
        Waiting for confirmation… <HexLink kind="tx" value={state.hash} />
      </Notice>
    );
  if (state.status === 'done')
    return (
      <Notice tone="good">
        Confirmed. <HexLink kind="tx" value={state.hash} />
      </Notice>
    );
  return <Notice tone="bad">{state.message}</Notice>;
}

// ---------------------------------------------------------------------------
// Form controls
// ---------------------------------------------------------------------------

const controlCls =
  'w-full rounded-lg border border-slate-700 bg-ink-950 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-600 focus:border-volt-500 focus:outline-none focus:ring-1 focus:ring-volt-500 disabled:opacity-50';

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium text-slate-300">{label}</span>
      {children}
      {hint && <span className="mt-1.5 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

export const Input = (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} className={cn(controlCls, props.className)} />;
export const Textarea = (props: TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} className={cn(controlCls, 'min-h-24 resize-y', props.className)} />;
export const Select = (props: SelectHTMLAttributes<HTMLSelectElement>) => <select {...props} className={cn(controlCls, props.className)} />;

// ---------------------------------------------------------------------------
// Hex / address display
// ---------------------------------------------------------------------------

export function CopyText({ value, display, className }: { value: string; display?: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      title={value}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard unavailable */
        }
      }}
      className={cn('inline-flex items-center gap-1.5 font-mono text-xs text-slate-300 hover:text-white', className)}
    >
      {display ?? shortHex(value)}
      {copied ? <Check className="h-3 w-3 text-emerald-400" aria-hidden /> : <Copy className="h-3 w-3 opacity-60" aria-hidden />}
      <span className="sr-only">{copied ? 'Copied' : 'Copy'}</span>
    </button>
  );
}

export function HexLink({ kind, value, className }: { kind: 'address' | 'tx' | 'block'; value: string; className?: string }) {
  return (
    <a
      href={explorerUrl(kind, value)}
      target="_blank"
      rel="noopener noreferrer"
      className={cn('inline-flex items-center gap-1 font-mono text-xs text-volt-400 hover:underline', className)}
    >
      {shortHex(value)}
      <ExternalLink className="h-3 w-3" aria-hidden />
    </a>
  );
}

export function KeyValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-slate-800/80 py-2.5 text-sm last:border-b-0">
      <dt className="shrink-0 text-slate-400">{label}</dt>
      <dd className="min-w-0 break-words text-right text-slate-100">{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialog (native <dialog>: focus trap, Esc to close)
// ---------------------------------------------------------------------------

export function Dialog({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-xl border border-slate-700 bg-ink-900 p-0 text-slate-100 backdrop:bg-black/70 backdrop:backdrop-blur-sm"
    >
      <div className="p-6">
        <h2 className="mb-4 text-lg font-semibold">{title}</h2>
        {children}
      </div>
    </dialog>
  );
}