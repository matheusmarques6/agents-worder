"use client";

import { createContext, useContext, type ComponentPropsWithoutRef } from "react";

// The Glass primitive — E0-14.
//
// Section 03 of the design gives three levels and one prohibition: glass never
// stacks. A glass card inside a glass card becomes a solid surface with no
// blur, or the pile turns milky.
//
// That prohibition is the entire reason this component exists. Anyone can
// write a blurred div; what cannot be left to whoever is building a screen at
// 6pm is remembering that the panel they are dropping into a card is already
// inside glass. So a Glass announces itself to its subtree, and any Glass that
// finds an announcement renders as the flat surface instead.
//
// Everything visual lives in app/globals.css, keyed off `data-glass` — this
// file holds no colour, no blur and no radius.

const InsideGlass = createContext(false);

export type GlassLevel = "chrome" | "card" | "overlay";

type GlassProps = ComponentPropsWithoutRef<"div"> & {
  /** chrome = sidebar and topbar · card = cards, panels, tables · overlay = modals, popovers, menus, toasts. */
  level?: GlassLevel;
};

/** Where the stacking rule stops applying.
 *
 * An overlay opened from inside a card is not stacked on that card: it floats
 * over the page, and its backdrop-filter samples the page. React context does
 * not know that — it travels through portals and through the top layer just
 * the same — so every overlay marks the boundary explicitly, and anything
 * inside it starts counting from zero again.
 *
 * The one legitimate way to escape the rule. A component that reaches for it
 * without being an overlay is working around the rule, not applying it. */
export function GlassBoundary({ children }: { children: React.ReactNode }) {
  return <InsideGlass.Provider value={false}>{children}</InsideGlass.Provider>;
}

export function Glass({ level = "card", children, ...rest }: GlassProps) {
  const nested = useContext(InsideGlass);

  return (
    <InsideGlass.Provider value={true}>
      <div data-glass={level} data-nested={nested ? "" : undefined} {...rest}>
        {children}
      </div>
    </InsideGlass.Provider>
  );
}
