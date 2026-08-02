import { expect, test, type Locator } from "@playwright/test";

import { canonical } from "./support/css";

// E0-14 — the Glass primitive and the rule it enforces.
//
// Section 03 of the design gives three levels and one prohibition: glass never
// stacks. A glass card inside a glass card becomes a solid surface with no
// blur, or the stack turns milky. That rule is the reason this component
// exists at all — anyone can write a blurred div; what cannot be left to
// discipline is remembering not to nest one.
//
// Asserted through computed style rather than a screenshot on purpose: the
// pixel of a blur is the one thing that is not deterministic (R1), while
// `backdrop-filter` as the browser computed it is exact. The visual baseline
// of the glass arrives with the harness in E0-17.

const SHOWCASE = "/design";

// The recipe, transcribed from section 03 and compared canonically — the build
// rewrites these to 8-digit hex, which quantises alpha to a byte
// (e2e/support/css.ts).
const LEVELS = {
  chrome: {
    blur: "blur(28px)",
    radius: "20px",
    from: "rgba(255, 255, 255, 0.075)",
    to: "rgba(255, 255, 255, 0.03)",
    border: "rgba(255, 255, 255, 0.1)",
  },
  card: {
    blur: "blur(24px)",
    radius: "18px",
    from: "rgba(255, 255, 255, 0.06)",
    to: "rgba(255, 255, 255, 0.022)",
    border: "rgba(255, 255, 255, 0.09)",
  },
  overlay: {
    blur: "blur(40px)",
    radius: "22px",
    from: "rgba(255, 255, 255, 0.1)",
    to: "rgba(255, 255, 255, 0.045)",
    border: "rgba(255, 255, 255, 0.15)",
  },
};

// "vira superfície sólida (rgba(255,255,255,.045), sem blur)" — the design's
// own words and its own value.
const NESTED_SURFACE = "rgba(255, 255, 255, 0.045)";

async function styleOf(element: Locator, property: string): Promise<string> {
  const value = await element.evaluate(
    (node, name) => getComputedStyle(node as Element).getPropertyValue(name),
    property,
  );
  return canonical(value);
}

test.describe("glass primitive", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(SHOWCASE);
  });

  for (const [level, recipe] of Object.entries(LEVELS)) {
    test(`${level} carries the blur, radius and translucency of its level`, async ({ page }) => {
      const glass = page.getByTestId(`glass-${level}`);

      expect(await styleOf(glass, "backdrop-filter")).toBe(recipe.blur);
      expect(await styleOf(glass, "border-radius")).toBe(recipe.radius);
      expect(await styleOf(glass, "border-top-color")).toBe(canonical(recipe.border));

      const surface = await styleOf(glass, "background-image");
      expect(surface).toContain(canonical(recipe.from));
      expect(surface).toContain(canonical(recipe.to));
    });
  }

  test("a glass inside a glass stops being glass", async ({ page }) => {
    const inner = page.getByTestId("glass-nested-inner");

    // The whole point: the inner one is a plain translucent surface. If this
    // ever reads a blur again, two layers of backdrop-filter are stacking and
    // the design's "leitosa" is back.
    expect(await styleOf(inner, "backdrop-filter")).toBe("none");
    expect(await styleOf(inner, "background-image")).toBe("none");
    expect(await styleOf(inner, "background-color")).toBe(canonical(NESTED_SURFACE));
  });

  test("the outer glass of a nested pair is untouched", async ({ page }) => {
    const outer = page.getByTestId("glass-nested-outer");

    // Otherwise the rule could be "implemented" by turning off both.
    expect(await styleOf(outer, "backdrop-filter")).toBe(LEVELS.card.blur);
  });

  test("nesting is detected at any depth, not just one level down", async ({ page }) => {
    // The context travels; a glass wrapped in a div wrapped in a glass is still
    // nested, and that is the case a naive `direct child` selector misses.
    const deep = page.getByTestId("glass-nested-deep");

    expect(await styleOf(deep, "backdrop-filter")).toBe("none");
  });

  test("a glass keeps its own radius when nested", async ({ page }) => {
    // Losing the blur is the rule; losing the shape would be a bug.
    const inner = page.getByTestId("glass-nested-inner");

    expect(await styleOf(inner, "border-radius")).toBe(LEVELS.card.radius);
  });
});

test.describe("the showcase is not part of production", () => {
  test("responds 404 when the environment flag is absent", async ({ page }) => {
    // Served by a second Next server started without DESIGN_SHOWCASE, so this
    // is the real production behaviour and not a mocked one. The decision is
    // server-side: the backend does not trust the frontend, not even here.
    const response = await page.goto(`http://127.0.0.1:3001${SHOWCASE}`);

    expect(response?.status()).toBe(404);
  });
});
