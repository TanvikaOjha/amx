import type { Metadata } from 'next';
import { GeistMono } from 'geist/font/mono';
import { GeistSans } from 'geist/font/sans';
import { Footer } from '@/components/Footer';
import { Navbar } from '@/components/Navbar';
import { Providers } from '@/components/Providers';
import { Suspense } from 'react';
import './global.css';

export const metadata: Metadata = {
  title: { default: 'Agent-Mesh-X', template: '%s · Agent-Mesh-X' },
  description: 'Pay-per-call AI agents with ERC-8004 identity, HTTP 402 payment vouchers and Hedera audit proofs.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body className="font-sans">
        <Providers>
          <Suspense fallback={<div className="h-16" />}>
          <Navbar />
          </Suspense>
          <main className="mx-auto min-h-[70vh] max-w-7xl px-4 py-10 sm:px-6">{children}</main>
          <Footer />
        </Providers>
      </body>
    </html>
  );
}
