"use client";

import { useState } from "react";

// The showcase's theme control — E0-16.
//
// It exists for one reader: whoever reviews a component PR and has to see the
// thing in both themes. Without it, checking light means opening a console and
// setting an attribute by hand, which is the kind of friction that ends with
// nobody checking light at all.
//
// Deliberately without persistence, without a system-preference read and
// without a cookie. This is a switch on a wall in a workshop, not a product
// preference — the product's own theme decision belongs to E5, with a user to
// store it against.

type Theme = "dark" | "light";

export function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>("dark");

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    setTheme(next);
  }

  return (
    <button
      type="button"
      data-testid="theme-switch"
      onClick={toggle}
      aria-label={`Mudar para o tema ${theme === "dark" ? "light" : "dark"}`}
      className="min-h-touch rounded-control border border-glass-card-border px-cards text-small text-fg-muted"
    >
      Tema · <span className="font-mono text-fg">{theme}</span>
    </button>
  );
}
