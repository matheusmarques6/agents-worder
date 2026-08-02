import type { ReactNode } from "react";

import { Glass } from "@/components/glass";

// Navigation — design §08.
//
// Both blocks always render; the stylesheet decides which one is on screen at
// the 860px line. A JS-switched layout would make the viewport tests pass
// while proving nothing about the CSS a merchant actually receives — and it
// would ship a phone a sidebar for the first paint.
//
// Sidebar, topbar and tab bar are all `chrome` glass. They are siblings, never
// nested: a topbar inside a sidebar would be caught by the stacking rule and
// lose its blur, which is the rule doing its job on a layout mistake.

export function Sidebar({ children, ...rest }: { children: ReactNode; "data-testid"?: string }) {
  return (
    <Glass level="chrome" data-sidebar="" {...rest}>
      {children}
    </Glass>
  );
}

export function StoreSwitcher({ label, name }: { label: string; name: string }) {
  return (
    <button type="button" data-switcher="">
      <span className="flex flex-col">
        <span data-switcher-label="">{label}</span>
        <span data-switcher-name="">{name}</span>
      </span>
      <span aria-hidden="true">⌄</span>
    </button>
  );
}

export function NavGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-chip">
      <div data-nav-group="">{label}</div>
      <div className="flex flex-col gap-[2px]" role="list">
        {children}
      </div>
    </div>
  );
}

export function NavItem({
  children,
  current = false,
  badge,
  ...rest
}: {
  children: ReactNode;
  current?: boolean;
  badge?: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <button
      type="button"
      role="listitem"
      data-nav-item=""
      aria-current={current ? "page" : undefined}
      {...rest}
    >
      {current ? <span data-nav-dot="" aria-hidden="true" /> : null}
      <span className="flex-1">{children}</span>
      {badge}
    </button>
  );
}

export function Topbar({ children }: { children: ReactNode }) {
  return (
    <Glass level="chrome" data-topbar="">
      {children}
    </Glass>
  );
}

export function Segmented({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div data-segment="" role="group" aria-label={label}>
      {children}
    </div>
  );
}

export function SegmentedItem({
  children,
  selected = false,
  ...rest
}: {
  children: ReactNode;
  selected?: boolean;
  "data-testid"?: string;
}) {
  return (
    <button type="button" data-segment-item="" aria-pressed={selected} {...rest}>
      {children}
    </button>
  );
}

export function TabBar({
  label,
  children,
  ...rest
}: {
  label: string;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <Glass level="chrome" data-tabbar="" role="navigation" aria-label={label} {...rest}>
      {children}
    </Glass>
  );
}

export function TabBarItem({
  children,
  current = false,
  ...rest
}: {
  children: ReactNode;
  current?: boolean;
  "data-testid"?: string;
}) {
  return (
    <button
      type="button"
      data-tabbar-item=""
      aria-current={current ? "page" : undefined}
      {...rest}
    >
      <span data-tabbar-icon="" aria-hidden="true" />
      {children}
    </button>
  );
}
