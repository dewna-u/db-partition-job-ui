import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        pj: {
          bg: "var(--pj-bg)",
          card: "var(--pj-card)",
          line: "var(--pj-line)",
          ink: "var(--pj-ink)",
          muted: "var(--pj-muted)",
          primary: "var(--pj-primary)",
          lime: "var(--pj-lime)",
          sidebar: "var(--pj-sidebar)",
          "sidebar-border": "var(--pj-sidebar-border)",
          "sidebar-muted": "var(--pj-sidebar-muted)",
          surface: "var(--pj-surface)",
          "table-head": "var(--pj-table-head)",
          ok: "var(--pj-ok)",
          warn: "var(--pj-warn)",
          fail: "var(--pj-fail)",
          "create-bg": "var(--pj-create-bg)",
          "drop-bg": "var(--pj-drop-bg)",
          "ok-bg": "var(--pj-ok-bg)",
          "warn-bg": "var(--pj-warn-bg)",
          "fail-bg": "var(--pj-fail-bg)",
        },
      },
      boxShadow: {
        card: "0 7px 22px color-mix(in srgb, var(--pj-ink) 4%, transparent)",
      },
      borderRadius: {
        card: "13px",
      },
    },
  },
  plugins: [],
} satisfies Config;
