import type { ComponentPropsWithoutRef } from "react";

// Field — design §06.
//
// A label, a control and, when something is wrong, a message. The three travel
// together because separating them is how a form ends up with a red border and
// no explanation of what to type instead.
//
// The error is announced, not only coloured: `aria-invalid` plus
// `aria-describedby` pointing at the message. Colour alone excludes anyone
// using a screen reader and anyone who cannot distinguish the red.
//
// `id` is required rather than generated. A form field without a stable id is
// a field a label cannot point at, a test cannot find and an error message
// cannot describe.

type FieldProps = {
  id: string;
  label: string;
  /** Shown under the control. Turns into the error message when `error` is set. */
  help?: string;
  error?: boolean;
  children?: never;
};

type InputProps = FieldProps & Omit<ComponentPropsWithoutRef<"input">, "id" | "children">;
type TextareaProps = FieldProps & Omit<ComponentPropsWithoutRef<"textarea">, "id" | "children">;

function Shell({
  id,
  label,
  help,
  error,
  control,
}: Omit<FieldProps, "children"> & { control: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-chip">
      <label data-field-label="" htmlFor={id}>
        {label}
      </label>
      {control}
      {help ? (
        <span data-field-help="" data-tone={error ? "error" : undefined} id={`${id}-help`}>
          {help}
        </span>
      ) : null}
    </div>
  );
}

export function InputField({ id, label, help, error, ...rest }: InputProps) {
  return (
    <Shell
      id={id}
      label={label}
      help={help}
      error={error}
      control={
        <input
          id={id}
          data-field-control=""
          aria-invalid={error || undefined}
          aria-describedby={help ? `${id}-help` : undefined}
          {...rest}
        />
      }
    />
  );
}

export function TextareaField({ id, label, help, error, ...rest }: TextareaProps) {
  return (
    <Shell
      id={id}
      label={label}
      help={help}
      error={error}
      control={
        <textarea
          id={id}
          data-field-control=""
          aria-invalid={error || undefined}
          aria-describedby={help ? `${id}-help` : undefined}
          {...rest}
        />
      }
    />
  );
}
