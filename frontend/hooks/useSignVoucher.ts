'use client';

import { useCallback } from 'react';
import { useSignTypedData } from 'wagmi';
import { CONTRACT_ADDRESSES } from '@/lib/constants';
import { VOUCHER_TYPES, normalizeSignature, voucherDomain, type VoucherStruct } from '@/lib/eip712';

/** Signs a payment voucher with the connected wallet (EIP-712, domain from LOCAL config, never from the gateway). */
export function useSignVoucher() {
  const { signTypedDataAsync, isPending } = useSignTypedData();

  const signVoucher = useCallback(
    async (voucher: VoucherStruct) => {
      const signature = await signTypedDataAsync({
        domain: voucherDomain(CONTRACT_ADDRESSES.PAYMENT_CHANNEL),
        types: VOUCHER_TYPES,
        primaryType: 'Voucher',
        message: voucher,
      });
      return normalizeSignature(signature);
    },
    [signTypedDataAsync],
  );

  return { signVoucher, isSigning: isPending };
}