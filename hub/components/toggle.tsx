import type { ComponentPropsWithoutRef } from "react";

// Toggle — design §06.
//
// `role="switch"` with `aria-checked`, not a styled checkbox: a switch takes
// effect immediately and a checkbox waits for a submit, and assistive
// technology announces the two differently. Everything this product toggles —
// follow-up, "never say AI", a funnel — takes effect immediately.
//
// The drawing is 44×26. The touch target is not the drawing: the button around
// it carries the 44px minimum, so the switch stays exactly as designed and a
// thumb still lands on it.

type ToggleProps = Omit<ComponentPropsWithoutRef<"button">, "onChange" | "children"> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
};

export function Toggle({ checked, onCheckedChange, ...rest }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      data-toggle=""
      aria-checked={checked}
      onClick={() => onCheckedChange(!checked)}
      {...rest}
    >
      <span data-toggle-track="">
        <span data-toggle-knob="" />
      </span>
    </button>
  );
}
