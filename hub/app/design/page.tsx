import { notFound } from "next/navigation";

import { Glass } from "@/components/glass";

// The showcase — E0-16 skeleton, born here because E0-14 needs a real page in
// a real browser to assert against. Components join it lot by lot in E0-15.
//
// It is not part of the product. The gate is server-side and evaluated per
// request: `force-dynamic` stops Next from deciding at build time, so a
// deployment without the flag answers 404 for real rather than serving a
// statically prerendered page. E2E proves exactly that against a second server
// started without the flag — "out of production" is an assertion here, not a
// promise.
export const dynamic = "force-dynamic";

const ENABLED = "1";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-item">
      <h2 className="font-mono text-label tracking-[0.14em] uppercase text-fg-subtle">{title}</h2>
      {/* Capture background: flat and opaque on purpose. A blur photographed
          over a gradient is the least deterministic pixel in the system (R1),
          and the visual baselines that arrive in E0-17 have to compare glass,
          not the weather behind it. */}
      <div className="flex flex-wrap gap-cards rounded-card bg-surface-solid p-card">{children}</div>
    </section>
  );
}

export default function DesignShowcase() {
  if (process.env.DESIGN_SHOWCASE !== ENABLED) {
    notFound();
  }

  return (
    <main data-testid="design-showcase" className="flex flex-col gap-section p-section">
      <header className="flex flex-col gap-chip">
        <h1 className="text-title font-semibold tracking-[-0.015em]">Obsidian Glass</h1>
        <p className="text-small text-fg-muted">
          Vitrine interna do design system. Não faz parte do produto.
        </p>
      </header>

      <Section title="03 · Liquid Glass">
        <Glass level="chrome" data-testid="glass-chrome" className="w-[220px] p-card">
          <div className="text-card font-semibold">glass/chrome</div>
          <div className="text-small text-fg-subtle">Sidebar e topbar</div>
        </Glass>

        <Glass level="card" data-testid="glass-card" className="w-[220px] p-card">
          <div className="text-card font-semibold">glass/card</div>
          <div className="text-small text-fg-subtle">Cards, painéis e tabelas</div>
        </Glass>

        <Glass level="overlay" data-testid="glass-overlay" className="w-[220px] p-card">
          <div className="text-card font-semibold">glass/overlay</div>
          <div className="text-small text-fg-subtle">Modais, popovers e menus</div>
        </Glass>
      </Section>

      <Section title="03 · Vidro empilha uma vez só">
        <Glass level="card" data-testid="glass-nested-outer" className="w-[300px] p-card">
          <div className="text-card font-semibold">Externo — vidro</div>

          <Glass level="card" data-testid="glass-nested-inner" className="mt-item p-card">
            <div className="text-small text-fg-muted">Interno — superfície, sem blur</div>
          </Glass>

          {/* Wrapped in a plain div: nesting is about ancestry, not about being
              a direct child. A selector-based implementation would miss this. */}
          <div className="mt-item">
            <Glass level="overlay" data-testid="glass-nested-deep" className="p-card">
              <div className="text-small text-fg-muted">Interno a dois níveis de profundidade</div>
            </Glass>
          </div>
        </Glass>
      </Section>
    </main>
  );
}
