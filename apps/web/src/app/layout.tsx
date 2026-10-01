import type { Metadata } from "next";
import "./globals.css";
import { Inter, Outfit } from "next/font/google";
import React from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";

const inter = Inter({
  subsets: ["latin"],
  preload: true,
  display: "swap",
});

// Display face for headings, the road-sign feel.
const outfit = Outfit({
  subsets: ["latin"],
  preload: true,
  display: "swap",
  variable: "--font-display",
});

export const metadata: Metadata = {
  title: "Road Trip Planner",
  description:
    "Plan a road trip: route, fuel cost, rest stops, hotels, meals and a full budget.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.className} ${outfit.variable} font-sans`}>
        <NuqsAdapter>{children}</NuqsAdapter>
      </body>
    </html>
  );
}
