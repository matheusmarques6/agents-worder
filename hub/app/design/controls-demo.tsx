"use client";

import { useState } from "react";

import { Chip, ChipGroup, ChoiceCard, ChoiceGroup } from "@/components/choice";
import { Toggle } from "@/components/toggle";

// The interactive half of sections 06. State lives here, in the showcase, so
// the components themselves stay controlled — which is the shape E4's wizard
// and E5's settings screens need, and the shape that makes "single choice is
// exclusive" a property of the caller rather than a hidden internal rule.

const TONES = [
  { value: "amigavel", label: "Amigável", description: '"oi, tudo bem? 🧡"' },
  { value: "neutro", label: "Neutro", description: '"olá, como posso ajudar?"' },
  { value: "formal", label: "Formal", description: '"prezado cliente,"' },
];

const TOPICS = [
  { value: "rastreio", label: "Rastreio" },
  { value: "trocas", label: "Trocas" },
  { value: "duvidas", label: "Dúvidas de produto" },
  { value: "faq", label: "FAQ" },
];

export function ControlsDemo() {
  const [followup, setFollowup] = useState(true);
  const [neverSayAi, setNeverSayAi] = useState(false);
  const [tone, setTone] = useState("amigavel");
  const [topics, setTopics] = useState(["rastreio", "trocas"]);

  function toggleTopic(value: string) {
    setTopics((current) =>
      current.includes(value) ? current.filter((item) => item !== value) : [...current, value],
    );
  }

  return (
    <div className="flex w-full flex-col gap-card">
      <div className="flex items-center justify-between gap-cards">
        <div className="flex flex-col">
          <span className="text-small">Follow-up proativo</span>
          <span className="text-field-help text-fg-subtle">
            agente puxa conversa após silêncio
          </span>
        </div>
        <Toggle
          checked={followup}
          onCheckedChange={setFollowup}
          data-testid="toggle-followup"
          aria-label="Follow-up proativo"
        />
      </div>

      <div className="flex items-center justify-between gap-cards">
        <div className="flex flex-col">
          <span className="text-small">Nunca dizer que é IA</span>
          <span className="text-field-help text-fg-subtle">padrão ligado</span>
        </div>
        <Toggle
          checked={neverSayAi}
          onCheckedChange={setNeverSayAi}
          data-testid="toggle-never-say-ai"
          aria-label="Nunca dizer que é IA"
        />
      </div>

      <div className="flex flex-col gap-item">
        <div className="font-mono text-label uppercase tracking-[0.14em] text-fg-subtle">
          Escolha única (cards)
        </div>
        <ChoiceGroup label="Tom de voz">
          {TONES.map((option) => (
            <ChoiceCard
              key={option.value}
              value={option.value}
              label={option.label}
              description={option.description}
              checked={tone === option.value}
              onSelect={setTone}
              data-testid={`choice-${option.value}`}
            />
          ))}
        </ChoiceGroup>
      </div>

      <div className="flex flex-col gap-item">
        <div className="font-mono text-label uppercase tracking-[0.14em] text-fg-subtle">
          Chips múltiplos
        </div>
        <ChipGroup label="Assuntos que o agente atende">
          {TOPICS.map((option) => (
            <Chip
              key={option.value}
              value={option.value}
              label={option.label}
              pressed={topics.includes(option.value)}
              onToggle={toggleTopic}
              data-testid={`chip-${option.value}`}
            />
          ))}
        </ChipGroup>
      </div>
    </div>
  );
}
