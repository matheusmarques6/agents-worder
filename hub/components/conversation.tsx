"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/button";
import { Glass } from "@/components/glass";

// Conversation — design §10.
//
// The only surface where the product's actual content lives, and the one a
// merchant reads hundreds of times a day. Three channels carry meaning here
// and none of them is colour: position says which side a message came from,
// mono says which numbers can be scanned in a column, and words say whether it
// arrived.

export type BubbleKind = "inbound" | "agent" | "human";

export function Thread({ children }: { children: ReactNode }) {
  return <div data-thread="">{children}</div>;
}

export function InboundBubble({ children, ...rest }: { children: ReactNode; "data-testid"?: string }) {
  return (
    <div data-bubble="inbound" {...rest}>
      {children}
    </div>
  );
}

export function OutboundBubble({
  kind,
  meta,
  status,
  statusLabel,
  children,
  ...rest
}: {
  kind: Extract<BubbleKind, "agent" | "human">;
  /** Author and time, in mono — a column of these is scanned, not read. */
  meta: string;
  /** The delivery mark as drawn. */
  status?: string;
  /** What the mark means, in words. An icon alone tells a screen reader
   * nothing, and delivery is the one thing a merchant checks under pressure. */
  statusLabel?: string;
  children: ReactNode;
  "data-testid"?: string;
}) {
  // Derived rather than hardcoded: two bubbles in the same thread would
  // otherwise share a test id, and a locator that matches two elements is a
  // locator that proves nothing about either.
  const testid = rest["data-testid"];

  return (
    <div data-outbound="" {...rest}>
      <div data-bubble={kind}>{children}</div>
      <div data-bubble-meta="" data-testid={testid ? `${testid}-meta` : undefined}>
        {meta}
        {status ? (
          <>
            {" · "}
            <span
              data-testid={testid ? `${testid}-status` : undefined}
              aria-label={statusLabel}
              role="img"
            >
              {status}
            </span>
          </>
        ) : null}
      </div>
    </div>
  );
}

/** Where the agent stopped and a person started, inside the thread itself —
 * so the merchant scrolling back does not have to cross-reference a log. */
export function ThreadMarker({ children, ...rest }: { children: ReactNode; "data-testid"?: string }) {
  return (
    <div data-thread-marker="" {...rest}>
      <span data-thread-marker-dot="" aria-hidden="true" />
      {children}
    </div>
  );
}

export function TypingIndicator({ label }: { label: string }) {
  return (
    <div data-typing="" role="status" aria-label={label}>
      <span />
      <span />
      <span />
    </div>
  );
}

export function TakeoverBanner({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action: ReactNode;
}) {
  return (
    <div data-takeover="">
      <span className="flex flex-col gap-[2px]">
        <span data-alert-title="">{title}</span>
        <span data-takeover-body="">{description}</span>
      </span>
      {action}
    </div>
  );
}

type ComposerProps = {
  value: string;
  onValueChange: (value: string) => void;
  onSend?: () => void;
  /** While the agent is answering, a human typing into the same conversation
   * is a race nobody wins — so the composer is closed, not merely styled as
   * closed. */
  blocked?: boolean;
  blockedLabel?: string;
  placeholder?: string;
};

export function Composer({
  value,
  onValueChange,
  onSend,
  blocked = false,
  blockedLabel,
  placeholder,
}: ComposerProps) {
  if (blocked) {
    return (
      <div data-composer="blocked">
        <input
          data-field-control=""
          data-testid="composer-blocked-input"
          disabled
          value={blockedLabel ?? ""}
          readOnly
          aria-label={blockedLabel}
        />
      </div>
    );
  }

  return (
    <Glass level="chrome" data-composer="">
      <input
        data-field-control=""
        data-testid="composer-input"
        value={value}
        placeholder={placeholder}
        aria-label={placeholder}
        onChange={(event) => onValueChange(event.target.value)}
      />
      <Button
        data-composer-send=""
        data-testid="composer-send"
        aria-label="Enviar mensagem"
        disabled={value.trim().length === 0}
        onClick={onSend}
      >
        <span aria-hidden="true">→</span>
      </Button>
    </Glass>
  );
}
