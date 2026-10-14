import type { Metadata } from "next";
import Script from "next/script";
import { JetBrains_Mono, Manrope } from "next/font/google";
import { AppShell } from "@/components/shell/app-shell";
import { themeStorageKey } from "@/components/theme/theme-provider";
import "./globals.css";

const manrope = Manrope({
  variable: "--font-manrope",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const jetbrains = JetBrains_Mono({
  variable: "--font-jetbrains",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export const metadata: Metadata = {
  title: {
    template: "PartOps | %s",
    default: "PartOps",
  },
  description: "GTN EDB Partition Operations Platform",
  icons: {
    icon: "/branding/partops-mark.svg",
  },
};

const themeInitScript = `
(function () {
  try {
    var key = ${JSON.stringify(themeStorageKey)};
    var stored = localStorage.getItem(key);
    var mode = stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
    var dark =
      mode === "dark" ||
      (mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    if (dark) document.documentElement.classList.add("dark");
    else document.documentElement.classList.remove("dark");
  } catch (e) {}
})();
`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <Script id="partops-theme-init" strategy="beforeInteractive">
          {themeInitScript}
        </Script>
      </head>
      <body className={`${manrope.variable} ${jetbrains.variable} font-sans antialiased`}>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
