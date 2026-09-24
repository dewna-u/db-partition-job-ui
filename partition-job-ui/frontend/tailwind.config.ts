import type { Config } from "tailwindcss";

export default {
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
          bg: "#f3f0e9",
          card: "#fbfaf7",
          line: "#ddd9d0",
          ink: "#1c2730",
          muted: "#718078",
          primary: "#165dff",
          lime: "#e5ff5c",
          sidebar: "#202c34",
        },
      },
      boxShadow: {
        card: "0 7px 22px #4c554c09",
      },
      borderRadius: {
        card: "13px",
      },
    },
  },
  plugins: [],
} satisfies Config;
