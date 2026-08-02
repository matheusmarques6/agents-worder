// Comparing CSS values without comparing their spelling.
//
// Between what the design writes, what the build ships and what the browser
// computes, the same value takes three different forms:
//
//   design    rgba(255, 255, 255, 0.075)   #FFFFFF     -0.035em
//   build     #ffffff13                    #fff        -.035em
//   computed  rgba(255, 255, 255, 0.075)   rgb(...)    -0.035em
//
// The build step is the interesting one: Lightning CSS rewrites colours to
// their shortest form, and 8-digit hex quantises alpha to a byte — 0.075
// becomes 19/255 = 0.0745. That is a lossless-enough serialisation decision by
// the toolchain, not a change anyone made to the design, and it is exactly
// reproducible: every one of the twelve glass values round-trips through
// Math.round(alpha * 255).
//
// So tests keep the design's spelling and compare canonical forms: every
// colour becomes 8-digit hex, every number loses its cosmetic zeros.

function toHex8(colour: string): string {
  const hex = colour.match(/^#([0-9a-f]{3,8})$/i);
  if (hex) {
    const digits = hex[1];
    if (digits.length === 3 || digits.length === 4) {
      const expanded = [...digits].map((digit) => digit + digit).join("");
      return `#${expanded.length === 6 ? `${expanded}ff` : expanded}`.toLowerCase();
    }
    return `#${digits.length === 6 ? `${digits}ff` : digits}`.toLowerCase();
  }

  const functional = colour.match(/^rgba?\(([^)]*)\)$/i);
  if (!functional) return colour.toLowerCase();

  const parts = functional[1]
    .split(/[,/\s]+/)
    .map((part) => part.trim())
    .filter(Boolean);
  const [red, green, blue, alpha = "1"] = parts;

  const channel = (value: string) =>
    Math.round(value.endsWith("%") ? (parseFloat(value) / 100) * 255 : parseFloat(value))
      .toString(16)
      .padStart(2, "0");
  const alphaByte = Math.round(
    (alpha.endsWith("%") ? parseFloat(alpha) / 100 : parseFloat(alpha)) * 255,
  )
    .toString(16)
    .padStart(2, "0");

  return `#${channel(red)}${channel(green)}${channel(blue)}${alphaByte}`;
}

const COLOUR = /#[0-9a-f]{3,8}\b|rgba?\([^)]*\)/gi;

export function canonical(value: string): string {
  return value
    .replace(COLOUR, toHex8)
    .toLowerCase()
    .replace(/\s+/g, "")
    .replace(/(^|[^\w.])\.(\d)/g, "$10.$2") // .5 -> 0.5
    .replace(/(\.\d*?)0+\b/g, "$1") // 0.10 -> 0.1, never touching 10px
    .replace(/\.(?=\D|$)/g, ""); // 0. -> 0
}

export function canonicalAll(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).map(([name, value]) => [name, canonical(value)]),
  );
}
