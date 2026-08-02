import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // The build directory of the second E2E server — the one started without
    // the design-showcase flag to prove production answers 404 (E0-14). Same
    // reason `.next` is ignored: generated output is not source.
    ".next-production-check/**",
  ]),
]);

export default eslintConfig;
