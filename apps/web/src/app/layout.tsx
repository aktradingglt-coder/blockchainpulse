import type { Metadata } from "next";
import type { ReactNode } from "react";
import { loadProduct } from "@/lib/product";
import "./globals.css";

export function generateMetadata(): Metadata {
  const product = loadProduct();
  return { title: product.identity.product_name, description: "Evidence-backed multi-asset market research." };
}

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
