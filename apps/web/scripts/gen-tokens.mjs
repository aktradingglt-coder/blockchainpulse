// Generates src/generated/tokens.css from config/design_tokens.yaml so the YAML stays the single source of truth.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";

const here = dirname(fileURLToPath(import.meta.url));
const configDir = process.env.CONFIG_DIR ?? resolve(here, "../../../config");
const tokens = parse(readFileSync(resolve(configDir, "design_tokens.yaml"), "utf8"));
const vars = (theme) =>
  Object.entries(tokens.themes[theme])
    .map(([k, v]) => `  --rd-${k.replaceAll("_", "-")}: ${v};`)
    .join("\n");
const css = `/* Generated from config/design_tokens.yaml — do not edit. */
:root {
${vars("light")}
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
${vars("dark")}
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
${vars("dark")}
  color-scheme: dark;
}
`;
const out = resolve(here, "../src/generated/tokens.css");
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, css);
