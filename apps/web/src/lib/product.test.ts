import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
const { configDir, loadProduct } = await import("./product");

describe("loadProduct", () => {
  it("reads the repository product config", () => {
    const product = loadProduct(resolve(process.cwd(), "../../config"));
    expect(product.identity.primary_domain).toBe("example.com");
    expect(product.commercial.trial_days).toBe(7);
  });

  it("rejects an invalid support email", () => {
    const dir = mkdtempSync(resolve(tmpdir(), "rd-"));
    const src = readFileSync(resolve(process.cwd(), "../../config/product.yaml"), "utf8");
    writeFileSync(resolve(dir, "product.yaml"), src.replace("support@example.com", "not-an-email"));
    expect(() => loadProduct(dir)).toThrow();
  });

  it("honours CONFIG_DIR", () => {
    process.env.CONFIG_DIR = "/tmp/somewhere";
    expect(configDir()).toBe("/tmp/somewhere");
    delete process.env.CONFIG_DIR;
  });
});
