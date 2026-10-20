'use client';

import { useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { Hash, TransactionReceipt } from 'viem';
import { errorMessage, publicClient } from '@/lib/chain';

export type TxState =
  | { status: 'idle' }
  | { status: 'signing' }
  | { status: 'confirming'; hash: Hash }
  | { status: 'done'; hash: Hash }
  | { status: 'error'; message: string };

/**
 * Wraps "send a transaction, wait for it" with UI state.
 *   const receipt = await run(() => writeContractAsync({ ... }));
 */
export function useTx() {
  const [state, setState] = useState<TxState>({ status: 'idle' });
  const queryClient = useQueryClient();

  const run = useCallback(
    async (submit: () => Promise<Hash>): Promise<TransactionReceipt | null> => {
      setState({ status: 'signing' });
      try {
        const hash = await submit();
        setState({ status: 'confirming', hash });
        const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 180_000 });
        if (receipt.status !== 'success') throw new Error('The transaction reverted.');
        setState({ status: 'done', hash });
        await queryClient.invalidateQueries({ queryKey: ['amx'] });
        return receipt;
      } catch (err) {
        setState({ status: 'error', message: errorMessage(err) });
        return null;
      }
    },
    [queryClient],
  );

  const reset = useCallback(() => setState({ status: 'idle' }), []);
  const busy = state.status === 'signing' || state.status === 'confirming';
  return { state, run, reset, busy };
}