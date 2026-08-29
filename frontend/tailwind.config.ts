import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#0c0d0f",
        "bg-soft": "#101114",
        panel: "rgba(255,255,255,0.025)",
        "panel-strong": "#141518",
        line: "rgba(255,255,255,0.07)",
        "line-strong": "rgba(255,255,255,0.13)",
        mute: "#8b9099",
        faint: "#5b616a",
        accent: "#76b900",
        ok: "#34d399",
        warn: "#fbbf24",
        err: "#f87171",
        info: "#60a5fa",
      },
      fontFamily: {
        sans: [
          "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Roboto",
          "Helvetica Neue", "Arial", "PingFang SC", "Microsoft YaHei", "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: {
        panel: "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px rgba(0,0,0,0.35)",
        pop: "0 4px 20px rgba(0,0,0,0.5)",
      },
    },
  },
  plugins: [],
};

export default config;
