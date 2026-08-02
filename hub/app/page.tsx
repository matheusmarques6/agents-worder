// Placeholder home. It exists so the E2E level has something real to open
// (E0-09) and for no other reason — the designed screens are explicitly out of
// scope for E0. It carries no token, no glass and no brand colour: those
// arrive with the design system in T3, and this page is rebuilt on top of them
// then. The only contract here is `data-testid="hub-home"`, chosen so the
// journey survives that rebuild without being edited.

export default function Home() {
  return (
    <main
      data-testid="hub-home"
      className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center"
    >
      <h1 className="text-2xl font-semibold tracking-tight">Agents Worder</h1>
      <p className="max-w-prose text-sm opacity-70">
        Agentes de IA no WhatsApp para recuperar vendas e atender clientes.
      </p>
    </main>
  );
}
