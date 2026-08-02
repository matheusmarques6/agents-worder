"use client";

import { useEffect, useRef, type ReactNode } from "react";

import { Glass, GlassBoundary } from "@/components/glass";

// Overlays — design §11.
//
// Every component here marks the glass boundary. An overlay opened from inside
// a card is not stacked on that card: it floats over the page and its
// backdrop-filter samples the page. React context does not know that — it
// travels through portals and through the top layer just the same — so without
// the reset the panel would declare itself nested and give up the blur that
// makes it an overlay.
//
// The modal is the native `<dialog>` with `showModal()`. Focus trap, Escape
// and the top layer come with it; a library would add all three plus a
// dependency, and the burden of proof is on whoever wants the dependency.

export function ModalPanel({
  title,
  children,
  actions,
  ...rest
}: {
  title: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <Glass level="overlay" data-modal-panel="" {...rest}>
      <div className="flex flex-col gap-chip">
        <div data-modal-title="">{title}</div>
        <div data-modal-body="">{children}</div>
      </div>
      {actions ? <div data-modal-actions="">{actions}</div> : null}
    </Glass>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  actions,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;

    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);

  return (
    <dialog ref={dialog} data-modal="" data-testid="modal" onClose={onClose}>
      <GlassBoundary>
        <ModalPanel title={title} actions={actions} data-testid="modal-panel">
          {children}
        </ModalPanel>
      </GlassBoundary>
    </dialog>
  );
}

export function Menu({ label, children, ...rest }: { label: string; children: ReactNode; "data-testid"?: string }) {
  return (
    <GlassBoundary>
      <Glass level="overlay" data-menu="" role="menu" aria-label={label} {...rest}>
        {children}
      </Glass>
    </GlassBoundary>
  );
}

export function MenuItem({
  children,
  active = false,
  tone,
  ...rest
}: {
  children: ReactNode;
  active?: boolean;
  tone?: "danger";
  "data-testid"?: string;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      data-menu-item=""
      data-active={active || undefined}
      data-tone={tone}
      {...rest}
    >
      {children}
    </button>
  );
}

export function MenuSeparator() {
  return <div data-menu-separator="" role="separator" />;
}
