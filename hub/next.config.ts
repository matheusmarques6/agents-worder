import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Playwright drives the dev server through 127.0.0.1 (playwright.config.ts
  // baseURL), and Next warns on every request from an origin it was not told
  // about. A warning that is always there is a warning nobody reads.
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
