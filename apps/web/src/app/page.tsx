import { loadProduct } from "@/lib/product";

export const dynamic = "force-dynamic";

export default function Home() {
  const product = loadProduct();
  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-16">
      <h1 className="text-4xl font-semibold tracking-tight">{product.identity.product_name}</h1>
      <p className="text-lg text-muted">
        Evidence-backed research on FX, commodities, futures, stocks and crypto. Every number links to its source.
      </p>
      {product.identity.working ? (
        <p className="rounded-md border border-border bg-surface p-4 text-sm text-muted">
          Pre-launch build. Product name, company details and domain are working values pending confirmation.
          Contact: {product.identity.support_email}
        </p>
      ) : null}
    </main>
  );
}
