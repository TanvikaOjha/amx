import { fetchGatewayAgentCard } from './gateway';
import { GATEWAY_AGENT_ID } from './constants';

export interface AgentCardData {
  name?: string;
  description?: string;
  active?: boolean;
  services: { name: string; endpoint: string; version?: string }[];
  supportedTrust: string[];
  amx?: { paymentScheme?: string; network?: string; pricePerCall?: string; asset?: string; channelContract?: string };
}

const s = (v: unknown) => (typeof v === 'string' ? v.slice(0, 500) : undefined);

function normalise(raw: unknown): AgentCardData {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  const services = Array.isArray(r.services)
    ? r.services.flatMap((x) => {
        const o = x as Record<string, unknown>;
        return s(o?.endpoint) ? [{ name: s(o.name) ?? 'service', endpoint: s(o.endpoint) as string, version: s(o.version) }] : [];
      })
    : [];
  const amx = (typeof r.amx === 'object' && r.amx !== null ? r.amx : undefined) as Record<string, unknown> | undefined;
  return {
    name: s(r.name),
    description: s(r.description),
    active: typeof r.active === 'boolean' ? r.active : undefined,
    services,
    supportedTrust: Array.isArray(r.supportedTrust) ? r.supportedTrust.filter((t): t is string => typeof t === 'string').slice(0, 8) : [],
    amx: amx
      ? { paymentScheme: s(amx.paymentScheme), network: s(amx.network), pricePerCall: s(amx.pricePerCall), asset: s(amx.asset), channelContract: s(amx.channelContract) }
      : undefined,
  };
}

async function fetchWithTimeout(url: string, ms = 8000): Promise<unknown> {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(), ms);
  try {
    const res = await fetch(url, { signal: c.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(t);
  }
}

/** Supports data:, ipfs:// and http(s) URIs. A CORS failure on the gateway's own card falls back to the proxy. */
export async function fetchAgentCard(uri: string, agentId: bigint): Promise<AgentCardData> {
  if (!uri) throw new Error('This agent has no registration URI.');
  if (uri.startsWith('data:')) {
    const [meta, payload = ''] = [uri.slice(0, uri.indexOf(',')), uri.slice(uri.indexOf(',') + 1)];
    const text = meta.includes(';base64') ? atob(payload) : decodeURIComponent(payload);
    return normalise(JSON.parse(text));
  }
  const url = uri.startsWith('ipfs://') ? `https://ipfs.io/ipfs/${uri.slice(7)}` : uri;
  if (!/^https?:\/\//.test(url)) throw new Error('Unsupported agent URI scheme.');
  try {
    return normalise(await fetchWithTimeout(url));
  } catch (err) {
    if (agentId === GATEWAY_AGENT_ID) return normalise(await fetchGatewayAgentCard());
    throw err;
  }
}