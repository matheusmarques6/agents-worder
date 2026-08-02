import type { ReactNode } from "react";

import { Button } from "@/components/button";

// Data table — design §09.
//
// A real `<table>`, not a grid of divs: the header/cell relationship is what
// lets a screen reader say "Status: ativo" instead of reading a wall of text,
// and it is free with the element.
//
// Numeric columns are mono (princípio 03) — a column of durations in a
// proportional face cannot be compared down the page, which is the only reason
// the column exists.

export function DataTable({ caption, head, children }: { caption: string; head: string[]; children: ReactNode }) {
  return (
    <table data-table="">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          {head.map((column) => (
            <th key={column} scope="col">
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

export type CellKind = "default" | "muted" | "numeric";

export function Cell({
  kind = "default",
  children,
  ...rest
}: {
  kind?: CellKind;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <td data-table-cell={kind === "default" ? undefined : kind} {...rest}>
      {children}
    </td>
  );
}

export function Pagination({
  range,
  page,
  ...rest
}: {
  /** "1–3 de 128" — the count reads in mono, like every other number. */
  range: string;
  page: number;
  "data-testid"?: string;
}) {
  return (
    <div data-pagination="" {...rest}>
      <span data-pagination-count="">{range}</span>
      <div className="flex gap-chip">
        <Button variant="ghost" aria-label="Página anterior">
          ‹
        </Button>
        {/* aria-current is what announces "you are on this page" — the visual
            emphasis alone tells a screen reader nothing. */}
        <Button variant="ghost" aria-current="page" aria-label={`Página ${page}`}>
          {page}
        </Button>
        <Button variant="ghost" aria-label="Próxima página">
          ›
        </Button>
      </div>
    </div>
  );
}
