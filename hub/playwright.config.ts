import { defineConfig, devices } from "@playwright/test";

// Desktop and mobile are both first-class (core/telas-da-aplicacao.md, confirmed
// premise 3), so both are projects here rather than an afterthought: a journey
// that only passes on desktop does not pass.
//
// Visual baselines are produced by CI only. Locally a missing baseline FAILS
// instead of being written, because a screenshot taken on this machine —
// different font rasteriser, different GPU compositing of the glass blur —
// would silently become the contract everyone else is compared against.
const isCI = !!process.env.CI;

// A hub that is ALREADY running — the compose container, later a staging
// deploy — instead of the servers this config starts. Set it and the journeys
// run against that target and nothing is booted here.
//
// Two reasons it exists. The smoke test of E0-23 has to exercise a deployed
// hub, not a copy built on the spot; and on a machine where :3000 belongs to
// another project, `reuseExistingServer` would silently point the whole suite
// at that stranger's app and report on it. CI never sets this — the gate keeps
// owning its own servers, and the visual baselines with them.
const externalHub = process.env.HUB_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  workers: isCI ? 2 : undefined,
  reporter: isCI ? [["github"], ["html", { open: "never" }]] : [["list"]],

  // One baseline per project: <test-file>-snapshots/<name>-<project>.png
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{projectName}{ext}",

  // Locally the journeys run but screenshot assertions are skipped outright.
  // Playwright's default is to WRITE a missing baseline and fail — which is how a
  // local capture ends up committed as the contract. Skipping is the only way to
  // make "baselines come from CI" a property of the tool instead of a rule people
  // are asked to remember.
  ignoreSnapshots: !isCI,

  expect: {
    toHaveScreenshot: {
      // Not zero on purpose. `backdrop-filter` — the whole Obsidian Glass system
      // rests on it — is not deterministic to the pixel even on identical
      // hardware. A threshold this small still catches a 1px padding change;
      // raising it further to silence noise would be turning the gate off.
      maxDiffPixelRatio: 0.002,
      // Screenshots are a contract, not an animation frame.
      animations: "disabled",
      caret: "hide",
    },
  },

  use: {
    baseURL: externalHub ?? "http://127.0.0.1:3000",
    trace: "on-first-retry",
    // Deterministic rendering: no motion, fixed locale and timezone. Without the
    // last two, anything rendering a date or a currency differs per machine.
    contextOptions: { reducedMotion: "reduce" },
    locale: "pt-BR",
    timezoneId: "America/Sao_Paulo",
  },

  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      // Below the 860px breakpoint defined by the design system.
      name: "mobile",
      use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } },
    },
  ],

  // Two servers, because one of them has to be the production case.
  //
  // :3000 runs with the design showcase enabled — that is where the journeys
  // live. :3001 runs the same build WITHOUT the flag, and exists so "the
  // showcase is not part of production" can be asserted against a real server
  // answering a real request instead of being taken on trust.
  webServer: externalHub ? undefined : [
    {
      command: isCI ? "pnpm start" : "pnpm dev",
      url: "http://127.0.0.1:3000",
      env: { DESIGN_SHOWCASE: "1" },
      reuseExistingServer: !isCI,
      timeout: 120_000,
    },
    {
      command: isCI ? "pnpm exec next start --port 3001" : "pnpm exec next dev --port 3001",
      url: "http://127.0.0.1:3001",
      env: {
        // Explicitly empty rather than merely unset: a developer with the flag
        // exported in their shell would otherwise turn this server into a copy
        // of the first one and the 404 assertion would quietly stop testing.
        DESIGN_SHOWCASE: "",
        // Its own build directory — Next allows one dev server per directory.
        // In CI both servers are `next start` over the same build, so the
        // variable is only meaningful locally.
        ...(isCI ? {} : { NEXT_DIST_DIR: ".next-production-check" }),
      },
      reuseExistingServer: !isCI,
      timeout: 120_000,
    },
  ],
});
