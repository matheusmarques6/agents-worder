import type { Metadata } from "next";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import "./globals.css";

// Fonts come from the `geist` package, not from next/font/google: the files ship
// with the dependency and are pinned by the lockfile. A build-time download from
// Google would let the typeface drift between the machine that recorded a visual
// baseline and the one comparing against it — the single most common cause of
// flaky screenshot tests.

export const metadata: Metadata = {
  title: "Agents Worder",
  description: "Agentes de IA no WhatsApp para recuperar vendas e atender clientes.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="pt-BR"
      className={`${GeistSans.variable} ${GeistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
