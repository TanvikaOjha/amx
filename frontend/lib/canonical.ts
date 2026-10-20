/**
 * Canonical JSON: byte-identical to gateway/app/canonical.py and mcp_server/signer.py.
 * Sorted keys, no whitespace, UTF-8, integers only (|n| <= 2^53-1), no floats / NaN / lone surrogates.
 * The sha256 of this form is what a voucher's `requestHash` must equal.
 */
import { sha256, toBytes, type Hex } from 'viem';

export class CanonicalizationError extends Error {}

const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

/** Order by Unicode code point, as Python's sorted() does (JS default compares UTF-16 units). */
function compareCodePoints(a: string, b: string): number {
  const A = Array.from(a);
  const B = Array.from(b);
  for (let i = 0; i < Math.min(A.length, B.length); i++) {
    const d = (A[i].codePointAt(0) as number) - (B[i].codePointAt(0) as number);
    if (d !== 0) return d;
  }
  return A.length - B.length;
}

function encode(value: unknown, depth: number): string {
  if (depth > 32) throw new CanonicalizationError('nesting too deep');
  if (value === null) return 'null';
  switch (typeof value) {
    case 'boolean':
      return value ? 'true' : 'false';
    case 'number':
      if (!Number.isSafeInteger(value)) throw new CanonicalizationError('only integers within ±(2^53−1) are allowed');
      return String(value);
    case 'string':
      if (LONE_SURROGATE.test(value)) throw new CanonicalizationError('string is not valid UTF-8');
      return JSON.stringify(value);
    case 'object': {
      if (Array.isArray(value)) return `[${value.map((v) => encode(v, depth + 1)).join(',')}]`;
      const entries = Object.entries(value as Record<string, unknown>)
        .filter(([, v]) => v !== undefined)
        .sort(([a], [b]) => compareCodePoints(a, b));
      return `{${entries.map(([k, v]) => `${encode(k, depth + 1)}:${encode(v, depth + 1)}`).join(',')}}`;
    }
    default:
      throw new CanonicalizationError(`unsupported type: ${typeof value}`);
  }
}

export function canonicalJson(value: unknown): string {
  return encode(value, 0);
}

export function requestHash(body: unknown): Hex {
  return sha256(toBytes(canonicalJson(body)));
}