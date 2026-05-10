import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "@/styles/globals.css";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";

export const metadata: Metadata = {
  metadataBase: new URL("https://ytfactory.app"),
  title: {
    default: "ytFactory — An AI studio for YouTube Shorts",
    template: "%s · ytFactory",
  },
  description:
    "Describe a Short. ytFactory writes the script, casts the voice, generates the visuals, cuts the captions, ships it to YouTube. Built for operators who want to scale.",
  applicationName: "ytFactory",
  keywords: ["youtube shorts", "ai video studio", "ai shorts generator", "ytfactory"],
  openGraph: {
    type: "website",
    siteName: "ytFactory",
    title: "ytFactory — An AI studio for YouTube Shorts",
    description: "Describe a Short. We write, cast, render, ship.",
  },
  twitter: { card: "summary_large_image", title: "ytFactory" },
};

export const viewport: Viewport = {
  themeColor: "#0a0a0a",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`dark ${GeistSans.variable} ${GeistMono.variable}`}
      suppressHydrationWarning
    >
      <body className="min-h-screen bg-background font-sans antialiased">
        <TooltipProvider delayDuration={120}>{children}</TooltipProvider>
        <Toaster />
      </body>
    </html>
  );
}
