import type { Address, Hex } from 'viem';
import { CHAIN_ID, CONTRACT_ADDRESSES } from './constants';

/** Domain/types must match PaymentChannel.sol: EIP712("AMXPaymentChannel", "1") and VOUCHER_TYPEHASH. */
export function voucherDomain(verifyingContract: Address = CONTRACT_ADDRESSES.PAYMENT_CHANNEL) {
  return { name: 'AMXPaymentChannel', version: '1', chainId: CHAIN_ID, verifyingContract } as const;
}

export const EIP712_DOMAIN = voucherDomain();

export const VOUCHER_TYPES = {
  Voucher: [
    { name: 'channelId', type: 'bytes32' },
    { name: 'agentId', type: 'uint256' },
    { name: 'cumulativeAmount', type: 'uint256' },
    { name: 'nonce', type: 'uint64' },
    { name: 'expiry', type: 'uint64' },
    { name: 'requestHash', type: 'bytes32' },
  ],
} as const;

export interface VoucherStruct {
  channelId: Hex;
  agentId: bigint;
  cumulativeAmount: bigint;
  nonce: bigint;
  expiry: bigint;
  requestHash: Hex;
}

/** The JSON shape the gateway's PaymentVoucher schema accepts (amounts as decimal strings, ints as numbers). */
export interface WireVoucher {
  channelId: Hex;
  agentId: number;
  cumulativeAmount: string;
  nonce: number;
  expiry: number;
  requestHash: Hex;
}

export function toWireVoucher(v: VoucherStruct): WireVoucher {
  return {
    channelId: v.channelId,
    agentId: Number(v.agentId),
    cumulativeAmount: v.cumulativeAmount.toString(),
    nonce: Number(v.nonce),
    expiry: Number(v.expiry),
    requestHash: v.requestHash,
  };
}

/** Header value: base64url(JSON{voucher, signature}), unpadded. */
export function encodeTicket(voucher: WireVoucher, signature: Hex): string {
  const json = JSON.stringify({ voucher, signature }); // pure ASCII, so btoa is safe
  return btoa(json).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const HALF_N = 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0n;

/**
 * The gateway and the contract (OpenZeppelin ECDSA.recover) accept only 65-byte, low-s signatures with v in {27, 28}.
 * Fix v if a wallet returns 0/1, and refuse anything else instead of sending a ticket that will bounce.
 */
export function normalizeSignature(signature: Hex): Hex {
  const raw = signature.slice(2);
  if (raw.length !== 130) {
    throw new Error('This wallet returned a non-ECDSA signature. Smart-contract wallets are not supported by PaymentChannel.');
  }
  const r = raw.slice(0, 64);
  const s = raw.slice(64, 128);
  let v = parseInt(raw.slice(128), 16);
  if (v < 27) v += 27;
  if (v !== 27 && v !== 28) throw new Error('Unexpected signature recovery id.');
  if (BigInt(`0x${s}`) > HALF_N) throw new Error('The wallet produced a high-s signature, which the contract rejects. Try another wallet.');
  return `0x${r}${s}${v.toString(16)}` as Hex;
}