export const instant = false;

import AgentClient from './AgentClient';

export default async function AgentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  // Await the promise to resolve the Next.js 16 dynamic route params
  const resolvedParams = await params;
  
  return <AgentClient id={resolvedParams.id} />;
}