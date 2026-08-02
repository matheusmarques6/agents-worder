import type { ReactNode } from "react";

// Choice cards and chips — design §06.
//
// Two components that look almost alike and mean the opposite: a choice card
// is one of N, a chip is any of N. The difference is invisible in a
// screenshot, which is exactly why each carries the ARIA role that says which
// it is — `radio` inside a `radiogroup`, versus a toggle button with
// `aria-pressed`. Get this wrong and a keyboard user cannot tell either.

type ChoiceProps = {
  value: string;
  label: string;
  description?: string;
  checked: boolean;
  onSelect: (value: string) => void;
  "data-testid"?: string;
};

export function ChoiceGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="radiogroup" aria-label={label} className="flex gap-item">
      {children}
    </div>
  );
}

export function ChoiceCard({
  value,
  label,
  description,
  checked,
  onSelect,
  ...rest
}: ChoiceProps) {
  return (
    <button
      type="button"
      role="radio"
      data-choice=""
      aria-checked={checked}
      onClick={() => onSelect(value)}
      {...rest}
    >
      <span className="text-small font-semibold">{label}</span>
      {description ? <span data-choice-sub="">{description}</span> : null}
    </button>
  );
}

type ChipProps = {
  value: string;
  label: string;
  pressed: boolean;
  onToggle: (value: string) => void;
  "data-testid"?: string;
};

export function ChipGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-item">
      {children}
    </div>
  );
}

export function Chip({ value, label, pressed, onToggle, ...rest }: ChipProps) {
  return (
    <button
      type="button"
      data-chip=""
      aria-pressed={pressed}
      onClick={() => onToggle(value)}
      {...rest}
    >
      {label}
    </button>
  );
}
