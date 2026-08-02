import { notFound } from "next/navigation";

import { Button } from "@/components/button";
import { InputField, TextareaField } from "@/components/field";
import { Glass } from "@/components/glass";

import { ControlsDemo } from "./controls-demo";
import { ThemeSwitch } from "./theme-switch";

// The showcase — E0-16.
//
// It mirrors `Agents Worder - Design System.dc.html`: the same thirteen
// sections, the same numbers, the same order. That correspondence is the whole
// point — this is where a component PR is checked against the design, and a
// showcase organised differently from the document it mirrors stops being
// evidence and becomes decoration.
//
// It is not part of the product. The gate is server-side and evaluated per
// request: `force-dynamic` stops Next from deciding at build time, so a
// deployment without the flag answers 404 for real rather than serving a
// statically prerendered page. E2E proves exactly that against a second server
// started without the flag — "out of production" is an assertion here, not a
// promise.
export const dynamic = "force-dynamic";

const ENABLED = "1";

type Section = {
  id: string;
  title: string;
  note?: string;
  /** Which lot of E0-15 fills it, when it is still empty. */
  pending?: string;
};

const SECTIONS: Section[] = [
  { id: "01", title: "Cor", note: "laranja só em ação, estado ativo e dado" },
  { id: "02", title: "Tipografia", note: "Geist na interface · Geist Mono em número, ID e label" },
  { id: "03", title: "Liquid Glass", note: "três níveis · empilha uma vez só" },
  { id: "04", title: "Grade, raio e elevação" },
  { id: "05", title: "Botões", pending: "L1 · ação e entrada" },
  { id: "06", title: "Campos e controles", pending: "L1 · ação e entrada" },
  { id: "07", title: "Status, badges e feedback", pending: "L2 · status e feedback" },
  { id: "08", title: "Navegação", pending: "L3 · navegação e dados" },
  { id: "09", title: "Dados", pending: "L3 · navegação e dados" },
  { id: "10", title: "Conversa", pending: "L4 · conversa e sobreposição" },
  { id: "11", title: "Sobreposições", pending: "L4 · conversa e sobreposição" },
  { id: "12", title: "Tema light", note: "mesma estrutura · vidro branco sobre bege quente" },
  { id: "13", title: "Princípios" },
];

const BRAND = ["50", "100", "200", "300", "500", "600", "900"];

const TYPE_SCALE = [
  { token: "display · 44/600", className: "text-display font-semibold", sample: "Receita recuperada" },
  { token: "metric · 34/600", className: "text-metric font-semibold", sample: "R$ 84.320" },
  { token: "title · 19/600", className: "text-title font-semibold", sample: "Aprovação do agente" },
  { token: "card · 14/600", className: "text-card font-semibold", sample: "Funis de recuperação" },
  { token: "body · 13.5/400", className: "text-body", sample: "O agente responde sozinho." },
  { token: "small · 12/400", className: "text-small", sample: "Últimos 30 dias" },
  { token: "label · mono 10", className: "text-label font-mono uppercase", sample: "Janela" },
];

const RADII = ["chip", "control", "card", "chrome", "overlay", "pill"] as const;
const SPACE = ["chip", "item", "cards", "card", "section"] as const;
const MEASURES = [
  ["Sidebar", "242px"],
  ["Padding do shell", "14px"],
  ["Coluna lateral", "340px"],
  ["Largura máx. de leitura", "680px"],
  ["Breakpoint mobile", "< 860px"],
  ["Alvo de toque mín.", "44px"],
];

const PRINCIPLES = [
  ["Laranja é ação, não decoração", "Um único elemento laranja por bloco visual."],
  ["Vidro precisa de luz atrás", "Todo shell tem um glow laranja de fundo."],
  ["Número em mono, texto em Geist", "Valores, IDs, prazos e scores alinham em Geist Mono."],
];

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div className="font-mono text-label uppercase tracking-[0.14em] text-fg-subtle">{children}</div>
  );
}

/** The flat, opaque area a component is displayed on.
 *
 * Opaque on purpose: a blur photographed over a gradient is the least
 * deterministic pixel in the system (R1), and the visual baselines of E0-17
 * have to compare the glass, not the weather behind it. */
/** A labelled row of variants, the way section 05 lays them out. */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-item desk:flex-row desk:items-center desk:gap-cards">
      <span className="w-30 shrink-0 font-mono text-label text-fg-subtle">{label}</span>
      <div className="flex flex-wrap items-center gap-item">{children}</div>
    </div>
  );
}

function Stage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap gap-cards rounded-card bg-surface-solid p-card">{children}</div>
  );
}

function SectionShell({ section, children }: { section: Section; children?: React.ReactNode }) {
  return (
    <section
      id={`secao-${section.id}`}
      data-testid={`showcase-section-${section.id}`}
      className="flex scroll-mt-section flex-col gap-item"
    >
      <div className="flex flex-wrap items-baseline gap-item">
        <span className="font-mono text-label uppercase tracking-[0.16em] text-brand-500">
          {section.id}
        </span>
        <h2 className="text-title font-semibold tracking-[-0.015em]">{section.title}</h2>
        {section.note ? <span className="text-small text-fg-subtle">{section.note}</span> : null}
      </div>

      {children ?? (
        <Stage>
          <p className="text-small text-fg-subtle">Chega com o lote {section.pending}.</p>
        </Stage>
      )}
    </section>
  );
}

const CONTENT: Record<string, React.ReactNode> = {
  "01": (
    <Stage>
      <div className="flex w-full flex-col gap-item">
        <Label>Marca · brand</Label>
        <div className="flex flex-wrap gap-item">
          {BRAND.map((step) => (
            <div key={step} className="flex flex-col gap-chip">
              <div
                data-brand-step={step}
                className="h-12 w-16 rounded-control border border-glass-card-border"
                style={{ backgroundColor: `var(--color-brand-${step})` }}
              />
              <div className="font-mono text-label text-fg-subtle">{step}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="flex w-full flex-col gap-item">
        <Label>Semânticos · só em estado</Label>
        <div className="flex flex-wrap gap-item">
          <div className="flex items-center gap-chip text-small">
            <span className="size-3 rounded-pill bg-success" /> sucesso
          </div>
          <div className="flex items-center gap-chip text-small">
            <span className="size-3 rounded-pill bg-warning" /> atenção
          </div>
          <div className="flex items-center gap-chip text-small">
            <span className="size-3 rounded-pill bg-danger" /> erro
          </div>
        </div>
      </div>

      <div className="flex w-full flex-col gap-item">
        <Label>Superfícies e texto</Label>
        <div className="flex flex-wrap gap-item">
          <div className="h-12 w-24 rounded-control border border-glass-card-border bg-surface" />
          <div className="h-12 w-24 rounded-control border border-glass-card-border bg-surface-raised" />
          <div className="h-12 w-24 rounded-control border border-glass-card-border bg-surface-solid" />
          <div className="flex flex-col justify-center gap-chip text-small">
            <span className="text-fg">primário</span>
            <span className="text-fg-muted">secundário</span>
            <span className="text-fg-subtle">terciário</span>
            <span className="text-fg-disabled">desabilitado</span>
          </div>
        </div>
      </div>
    </Stage>
  ),

  "02": (
    <Stage>
      <div className="flex w-full flex-col gap-item">
        {TYPE_SCALE.map((entry) => (
          <div key={entry.token} className="flex flex-wrap items-baseline gap-cards">
            <span className="w-40 font-mono text-label text-fg-subtle">{entry.token}</span>
            <span className={entry.className}>{entry.sample}</span>
          </div>
        ))}
      </div>
    </Stage>
  ),

  "03": (
    <div className="flex flex-col gap-cards">
      <Stage>
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
      </Stage>

      <Stage>
        <Glass level="card" data-testid="glass-nested-outer" className="w-[300px] p-card">
          <div className="text-card font-semibold">Externo — vidro</div>

          <Glass level="card" data-testid="glass-nested-inner" className="mt-item p-card">
            <div className="text-small text-fg-muted">Interno — superfície, sem blur</div>
          </Glass>

          {/* Wrapped in a plain div: nesting is about ancestry, not about being
              a direct child. A selector-based implementation would miss this. */}
          <div className="mt-item">
            <Glass level="overlay" data-testid="glass-nested-deep" className="p-card">
              <div className="text-small text-fg-muted">Interno a dois níveis</div>
            </Glass>
          </div>
        </Glass>
      </Stage>
    </div>
  ),

  "04": (
    <Stage>
      <div className="flex w-full flex-col gap-item">
        <Label>Raio</Label>
        <div className="flex flex-wrap items-end gap-item">
          {RADII.map((radius) => (
            <div key={radius} className="flex flex-col items-center gap-chip">
              <div
                className="size-11 border border-glass-card-border bg-surface-raised"
                style={{ borderRadius: `var(--radius-${radius})` }}
              />
              <div className="font-mono text-label text-fg-subtle">{radius}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="flex w-full flex-col gap-item">
        <Label>Espaçamento · base 2</Label>
        <div className="flex flex-col gap-chip">
          {SPACE.map((step) => (
            <div key={step} className="flex items-center gap-cards">
              <div
                className="h-3 bg-brand-500"
                style={{ width: `var(--spacing-${step})` }}
              />
              <span className="font-mono text-label text-fg-subtle">{step}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="flex w-full flex-col gap-item">
        <Label>Layout do app</Label>
        <div className="flex flex-col gap-chip">
          {MEASURES.map(([name, value]) => (
            <div key={name} className="flex justify-between gap-cards text-small text-fg-muted">
              <span>{name}</span>
              <span className="font-mono text-fg-subtle">{value}</span>
            </div>
          ))}
        </div>
      </div>
    </Stage>
  ),

  "05": (
    <Stage>
      <div className="flex w-full flex-col gap-card">
        <Row label="primary">
          <Button data-testid="button-primary">Aprovar e ativar</Button>
          <Button state="hover">hover</Button>
          <Button state="pressed">pressed</Button>
          <Button loading data-testid="button-state-loading">
            salvando
          </Button>
          <Button disabled data-testid="button-state-disabled">
            desabilitado
          </Button>
        </Row>

        <Row label="secondary">
          <Button variant="secondary">Pausar agente</Button>
          <Button variant="secondary" state="hover">
            hover
          </Button>
          <Button variant="secondary" state="focus">
            focus
          </Button>
        </Row>

        <Row label="ghost · danger">
          <Button variant="ghost">Cancelar</Button>
          <Button variant="danger">Cancelar tenant</Button>
          <Button variant="danger-strong">Executar purga</Button>
        </Row>

        <Row label="tamanhos">
          <Button size="sm" data-testid="button-size-sm">
            sm · 30
          </Button>
          <Button size="md" data-testid="button-size-md">
            md · 38
          </Button>
          <Button size="lg" data-testid="button-size-lg">
            lg · 48 (mobile)
          </Button>
        </Row>
      </div>
    </Stage>
  ),

  "06": (
    <Stage>
      <div className="flex w-full max-w-reading flex-col gap-card">
        <InputField
          id="campo-nome"
          label="Nome do agente"
          defaultValue="Bela"
          data-testid="field-default-input"
        />
        <InputField
          id="campo-whatsapp"
          label="WhatsApp"
          defaultValue="+55 11 9"
          error
          help="Número incompleto — use DDD + 9 dígitos."
          data-testid="field-error-input"
        />
        <TextareaField
          id="campo-abertura"
          label="Frases de abertura"
          defaultValue="Oi! Aqui é a Bela da Bella Store 🧡 Em que posso ajudar?"
        />
      </div>

      <ControlsDemo />
    </Stage>
  ),

  "12": (
    <Stage>
      <p className="max-w-reading text-small text-fg-muted">
        O tema light tem paridade completa e é aplicado por <code className="font-mono">data-theme</code>{" "}
        no elemento raiz — nunca por preferência do sistema. Use o interruptor no topo para ver
        qualquer seção desta vitrine nos dois temas.
      </p>
    </Stage>
  ),

  "13": (
    <Stage>
      {PRINCIPLES.map(([title, body]) => (
        <Glass key={title} level="card" className="w-[300px] p-card">
          <div className="text-card font-semibold">{title}</div>
          <div className="mt-chip text-small text-fg-subtle">{body}</div>
        </Glass>
      ))}
    </Stage>
  ),
};

export default function DesignShowcase() {
  if (process.env.DESIGN_SHOWCASE !== ENABLED) {
    notFound();
  }

  return (
    <div
      data-testid="design-showcase"
      className="flex flex-col gap-section p-section desk:flex-row desk:items-start"
    >
      <nav
        data-testid="showcase-nav"
        className="flex flex-col gap-chip desk:sticky desk:top-section desk:w-sidebar desk:shrink-0"
      >
        <div className="flex flex-wrap items-center justify-between gap-item">
          <div>
            <div className="text-title font-semibold tracking-[-0.015em]">Obsidian Glass</div>
            <div className="text-small text-fg-subtle">Vitrine interna · fora do produto</div>
          </div>
          <ThemeSwitch />
        </div>

        <div className="flex flex-wrap gap-chip desk:flex-col">
          {SECTIONS.map((section) => (
            <a
              key={section.id}
              href={`#secao-${section.id}`}
              className="flex min-h-touch items-center gap-item rounded-control px-item text-small text-fg-muted"
            >
              <span className="font-mono text-label text-fg-subtle">{section.id}</span>
              {section.title}
            </a>
          ))}
        </div>
      </nav>

      <main className="flex min-w-0 flex-1 flex-col gap-section">
        {SECTIONS.map((section) => (
          <SectionShell key={section.id} section={section}>
            {CONTENT[section.id]}
          </SectionShell>
        ))}
      </main>
    </div>
  );
}
