import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Playwright drives the dev server through 127.0.0.1 (playwright.config.ts
  // baseURL), and Next warns on every request from an origin it was not told
  // about. A warning that is always there is a warning nobody reads.
  allowedDevOrigins: ["127.0.0.1"],

  // The E2E run starts a second server without the design-showcase flag, to
  // assert that production answers 404 for it. Next 16 allows only one dev
  // server per build directory — the lock is on the folder, not on the port —
  // so that second server gets its own. Unset everywhere else, which leaves
  // the default.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
