'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchHealth } from '@/lib/gateway';
import { Badge } from './ui';

export function useGatewayHealth() {
  return useQuery({ queryKey: ['amx', 'health'], queryFn: fetchHealth, refetchInterval: 15_000, retry: false });
}

/** Live /health from the gateway, through the same-origin proxy. */
export function GatewayStatusBadge() {
  const { data, isLoading, isError } = useGatewayHealth();
  if (isLoading) return <Badge>Checking gateway…</Badge>;
  if (isError || !data) return <Badge tone="bad">Gateway offline</Badge>;
  return <Badge tone={data.status === 'ok' ? 'good' : 'warn'}>Gateway {data.status === 'ok' ? 'online' : 'degraded'}</Badge>;
}
