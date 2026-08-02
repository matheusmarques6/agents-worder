"use client";

import { useState } from "react";

import { Button } from "@/components/button";
import {
  Composer,
  InboundBubble,
  OutboundBubble,
  Thread,
  ThreadMarker,
  TypingIndicator,
} from "@/components/conversation";
import { Glass } from "@/components/glass";
import { Menu, MenuItem, MenuSeparator, Modal } from "@/components/overlay";

// Sections 10 and 11 of the showcase.
//
// The card below is the point of the whole lot: a modal and a menu are opened
// from INSIDE a glass card, next to a plain nested glass. The overlays keep
// their blur, the nested glass loses its — the two halves of the stacking rule
// standing next to each other, where a reviewer can see both at once.

export function ConversationDemo() {
  const [draft, setDraft] = useState("");

  return (
    <div className="flex w-full flex-col gap-card desk:flex-row desk:items-start">
      <Glass level="card" className="flex-1 p-card">
        <Thread>
          <InboundBubble data-testid="bubble-inbound">
            Oi, meu pedido ainda não chegou 😕
          </InboundBubble>

          <OutboundBubble
            kind="agent"
            meta="agente · 14:02"
            status="✓✓"
            statusLabel="lida pelo contato"
            data-testid="bubble-outbound"
          >
            Oi, Marina! Seu pedido #4821 saiu para entrega hoje 🧡 Chega até amanhã.
          </OutboundBubble>

          <ThreadMarker data-testid="thread-takeover">
            Bruno assumiu a conversa · 14:03
          </ThreadMarker>

          <OutboundBubble kind="human" meta="humano · 14:04">
            Marina, sou o Bruno da loja — vou acompanhar pessoalmente.
          </OutboundBubble>

          <TypingIndicator label="O contato está digitando" />
        </Thread>
      </Glass>

      <div className="flex flex-1 flex-col gap-cards">
        <Composer
          value={draft}
          onValueChange={setDraft}
          placeholder="Escreva uma mensagem…"
        />
        <Composer
          value=""
          onValueChange={() => {}}
          blocked
          blockedLabel="Composer bloqueado — a IA está no controle"
        />
      </div>
    </div>
  );
}

export function OverlayDemo() {
  const [open, setOpen] = useState(false);

  return (
    <Glass level="card" className="flex w-full flex-col gap-card p-card">
      <div className="flex flex-wrap items-center gap-cards">
        <Button size="sm" data-testid="modal-trigger" onClick={() => setOpen(true)}>
          Abrir modal
        </Button>
        <span className="text-small text-fg-subtle">
          aberto de dentro deste card de vidro — e mantém o blur
        </span>
      </div>

      <Glass level="card" data-testid="overlay-nested-glass" className="p-card">
        <span className="text-small text-fg-muted">
          Vidro aninhado no mesmo card — perde o blur, como manda a regra
        </span>
      </Glass>

      <Menu label="Ações do agente" data-testid="menu-panel">
        <MenuItem active>Editar personalidade</MenuItem>
        <MenuItem data-testid="menu-item-versions">Ver versões</MenuItem>
        <MenuItem>Rodar cenários</MenuItem>
        <MenuSeparator />
        <MenuItem tone="danger">Desconectar número</MenuItem>
      </Menu>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Pausar o agente?"
        actions={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button onClick={() => setOpen(false)}>Pausar</Button>
          </>
        }
      >
        Enquanto pausado, nenhuma mensagem é respondida automaticamente. Os clientes continuam
        escrevendo e as conversas ficam no inbox.
      </Modal>
    </Glass>
  );
}
