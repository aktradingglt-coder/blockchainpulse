import "server-only";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import { z } from "zod";

// Mirrors the subset of config/product.yaml the web app renders. The Python loader is the authoritative validator.
const ProductSchema = z.object({
  identity: z.object({
    product_name: z.string().min(1),
    product_short_name: z.string().min(1),
    working: z.boolean(),
    legal_entity_name: z.string().min(1),
    support_email: z.string().email(),
    primary_domain: z.string().min(1),
  }),
  commercial: z.object({ trial_days: z.number().int().positive() }),
});

export type Product = z.infer<typeof ProductSchema>;

export function configDir(): string {
  return process.env.CONFIG_DIR ?? resolve(process.cwd(), "../../config");
}

export function loadProduct(dir: string = configDir()): Product {
  const raw: unknown = parse(readFileSync(resolve(dir, "product.yaml"), "utf8"));
  return ProductSchema.parse(raw);
}
