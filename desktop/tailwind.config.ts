import type { Config } from "tailwindcss";

const withAlpha = (variable: string): string =>
  `color-mix(in srgb, var(${variable}) calc(<alpha-value> * 100%), transparent)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: withAlpha("--lobster-background"),
        foreground: withAlpha("--lobster-foreground"),
        primary: {
          DEFAULT: withAlpha("--lobster-primary"),
          foreground: withAlpha("--lobster-primary-foreground"),
          hover: withAlpha("--lobster-primary-hover"),
          muted: withAlpha("--lobster-primary-muted"),
        },
        surface: {
          DEFAULT: withAlpha("--lobster-surface"),
          raised: withAlpha("--lobster-surface-raised"),
        },
        border: {
          DEFAULT: withAlpha("--lobster-border"),
          subtle: withAlpha("--lobster-border-subtle"),
        },
        secondary: withAlpha("--lobster-text-secondary"),
        muted: withAlpha("--lobster-text-muted"),
        destructive: withAlpha("--lobster-destructive"),
        success: withAlpha("--lobster-success"),
        warning: withAlpha("--lobster-warning"),
      },
      borderRadius: {
        theme: "var(--lobster-radius)",
      },
      boxShadow: {
        subtle: "var(--lobster-shadow-subtle)",
        card: "var(--lobster-shadow-card)",
        elevated: "var(--lobster-shadow-elevated)",
      },
    },
  },
  plugins: [],
} satisfies Config;
