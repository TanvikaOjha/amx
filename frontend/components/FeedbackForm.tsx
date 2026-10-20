'use client';

import { MessageSquarePlus } from 'lucide-react';
import { useState } from 'react';
import { useAccount } from 'wagmi';
import { useGiveFeedback, useMyFeedback } from '@/hooks/useAgentRegistry';
import { useWalletReady } from '@/hooks/useWalletReady';
import { Button, Dialog, Field, Input, Notice, TxStatus } from './ui';
import type { Address } from 'viem';

/** On-chain reputation: ERC8004Identity.giveFeedback(agentId, score 0–100, tag). One latest score per rater. */
export function FeedbackForm({ agentId, owner }: { agentId: bigint; owner: Address }) {
  const { address } = useAccount();
  const wallet = useWalletReady();
  const mine = useMyFeedback(agentId, address);
  const { giveFeedback, state, reset, busy } = useGiveFeedback();
  const [open, setOpen] = useState(false);
  const [score, setScore] = useState(80);
  const [tag, setTag] = useState('');

  const isOwner = !!address && address.toLowerCase() === owner.toLowerCase();
  const tagBytes = new TextEncoder().encode(tag).length;
  const tagTooLong = tagBytes > 64;

  const close = () => {
    setOpen(false);
    reset();
  };

  return (
    <>
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        <MessageSquarePlus className="h-4 w-4" aria-hidden />
        {mine.data != null ? 'Update your rating' : 'Rate this agent'}
      </Button>

      <Dialog open={open} onClose={close} title="Rate this agent">
        <div className="space-y-4">
          {!wallet.isConnected && <Notice tone="warn">Connect a wallet to leave a rating.</Notice>}
          {wallet.wrongNetwork && <Notice tone="warn">Switch your wallet to the app’s network first.</Notice>}
          {isOwner && <Notice tone="warn">You own this agent, so the contract will not accept a rating from you.</Notice>}
          {mine.data != null && <Notice>Your current rating is {mine.data}/100. Submitting again replaces it; it is never counted twice.</Notice>}

          <Field label={`Score: ${score}/100`}>
            <input type="range" min={0} max={100} value={score} onChange={(e) => setScore(Number(e.target.value))} className="w-full accent-cyan-400" />
          </Field>
          <Field label="Tag (optional)" hint={<span className={tagTooLong ? 'text-rose-300' : undefined}>{tagBytes}/64 bytes, e.g. “fast” or “accurate”.</span>}>
            <Input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="fast" maxLength={80} />
          </Field>

          <TxStatus state={state} />

          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              {state.status === 'done' ? 'Close' : 'Cancel'}
            </Button>
            <Button onClick={() => giveFeedback(agentId, score, tag.trim())} loading={busy} disabled={!wallet.ready || isOwner || tagTooLong || state.status === 'done'}>
              Submit rating
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}