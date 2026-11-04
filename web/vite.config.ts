/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), "VITE_"), ...process.env };
  const mock = env.VITE_MOCK === "1";
  const portal = env.PORTAL_URL || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    // service worker for msw is only needed (and only shipped) in mock mode
    publicDir: mock ? "public" : false,
    define: mock ? { "import.meta.env.VITE_MOCK": JSON.stringify("1") } : {},
    server: {
      port: 5173,
      proxy: mock
        ? undefined
        : {
            "/api": portal,
            "/auth": portal,
          },
    },
    build: { outDir: "dist", sourcemap: false, target: "es2022" },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/__tests__/**/*.test.{ts,tsx}"],
      css: false,
      testTimeout: 15000,
    },
  };
});
