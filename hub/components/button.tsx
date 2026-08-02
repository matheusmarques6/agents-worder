import type { ComponentPropsWithoutRef } from "react";

// Button — design §05.
//
// Five variants and three sizes, because that is what the design draws. The
// two danger levels are not a duplicate: the soft one is for a reversible
// destructive action ("pausar", "cancelar tenant"), the solid one for the
// irreversible one ("executar purga"). A single danger button would make the
// two read alike, which is the one place in this product where they must not.
//
// It holds no colour and no size: everything visual is in app/globals.css,
// keyed off `data-variant` and `data-size`.

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "danger-strong";
export type ButtonSize = "sm" | "md" | "lg";

/** A pseudo-class rendered as an attribute, so the showcase can display it.
 *
 * Only the showcase should pass this: a real hover cannot be screenshotted
 * deterministically, and a state that cannot be shown is a state nobody
 * reviews. In product code the browser drives these. */
export type ForcedState = "hover" | "pressed" | "focus" | "disabled";

type ButtonProps = ComponentPropsWithoutRef<"button"> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  state?: ForcedState;
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  state,
  disabled,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      data-button=""
      data-variant={variant}
      data-size={size}
      data-state={state}
      // A loading button that stays clickable sends the request twice, and in
      // this product a duplicated request is a duplicated WhatsApp message.
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span data-spinner="" aria-hidden="true" /> : null}
      {children}
    </button>
  );
}
