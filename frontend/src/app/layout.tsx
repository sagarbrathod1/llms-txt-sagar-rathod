import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "llms.txt generator",
  description: "Generate an llms.txt for any website and keep it up to date as the site changes.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} antialiased`}>
      <body className="min-h-screen bg-background font-sans text-foreground">
        {/* No border or nav: the wordmark is the only way home, and the page content leads. */}
        <header className="mx-auto max-w-2xl px-4 pt-6">
          <Link href="/" className="font-mono text-sm font-medium">
            llms.txt
          </Link>
        </header>
        <main className="mx-auto max-w-2xl px-4 pt-12 pb-24">{children}</main>
      </body>
    </html>
  );
}
