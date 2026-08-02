import type { ReactNode } from "react";

import { Glass } from "@/components/glass";

// Status, badges and feedback — design §07 (plus the skeleton drawn in §09 and
// the empty state in §11).
//
// The one rule the whole lot serves: a semantic colour carries state and
// nothing else (princípio 01). Its corollary is that colour is never the only
// channel — every badge here renders its label, because eight percent of men
// cannot separate the green from the red, and because a dashboard read at a
// glance is read by people, not by pixels.

/** The five values of `tenants.status` (migration 0001) — verbatim, so a
 * badge can never show a state the database cannot hold. Identifiers in
 * English like everything else in the codebase; the copy is the caller's. */
export type TenantStatus = "active" | "paused" | "onboarding" | "shadow" | "cancelled";

/** Shadow is drawn without a dot — its dashed border is the signal. */
const WITHOUT_DOT: TenantStatus[] = ["shadow"];

export function StatusBadge({
  status,
  children,
  ...rest
}: {
  status: TenantStatus;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <span data-status={status} {...rest}>
      {WITHOUT_DOT.includes(status) ? null : <span data-status-dot="" aria-hidden="true" />}
      {children}
    </span>
  );
}

export type TechTone = "count" | "strong" | "neutral" | "success" | "danger";

export function TechBadge({
  tone = "neutral",
  children,
  ...rest
}: {
  tone?: TechTone;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <span data-tech={tone} {...rest}>
      {children}
    </span>
  );
}

export type AlertTone = "success" | "warning" | "danger";

type AlertProps = {
  tone: AlertTone;
  title: ReactNode;
  children?: ReactNode;
  "data-testid"?: string;
};

/** `alert` interrupts a screen reader, `status` waits its turn. That is the
 * difference between "your number is disconnected" and "saved". */
function roleFor(tone: AlertTone) {
  return tone === "success" ? "status" : "alert";
}

function Body({ tone, title, children }: Omit<AlertProps, "data-testid">) {
  return (
    <span data-alert={tone}>
      <span data-alert-dot="" aria-hidden="true" />
      <span data-alert-text="">
        <span data-alert-title="">{title}</span>
        {children ? <span data-alert-body="">{children}</span> : null}
      </span>
    </span>
  );
}

export function Alert({ tone, title, children, ...rest }: AlertProps) {
  return (
    <div role={roleFor(tone)} {...rest}>
      <Body tone={tone} title={title}>
        {children}
      </Body>
    </div>
  );
}

/** The toast is the success alert drawn as overlay glass: same anatomy, a
 * surface that floats. Composing <Glass> rather than restating the recipe is
 * what keeps it under the rule that glass never stacks — and the alert body
 * stays a separate element so the glass keeps its own edge. */
export function Toast({ title, children, ...rest }: Omit<AlertProps, "tone">) {
  return (
    <Glass level="overlay" data-toast="" role="status" {...rest}>
      <Body tone="success" title={title}>
        {children}
      </Body>
    </Glass>
  );
}

export function ConnectionStatus({ children }: { children: ReactNode }) {
  return (
    <div data-connection="" role="status">
      <span data-connection-dots="" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      {children}
    </div>
  );
}

/** A shape standing in for content that does not exist yet — announcing it
 * would read as gibberish. */
export function Skeleton({ width, ...rest }: { width?: string; "data-testid"?: string }) {
  return <span data-skeleton="" aria-hidden="true" style={{ width }} {...rest} />;
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: ReactNode;
  description: ReactNode;
  /** Optional, and the design draws it without one. When a screen does have a
   * next step, it passes the Button from L1 — an empty state never grows a
   * button of its own. */
  action?: ReactNode;
}) {
  return (
    <div data-empty="">
      <span data-empty-icon="" aria-hidden="true" />
      <span data-empty-title="">{title}</span>
      <span data-empty-body="">{description}</span>
      {action}
    </div>
  );
}
