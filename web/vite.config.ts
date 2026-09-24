import { defineConfig } from "vitest/config";

// 开发时前端跑在 Vite，/api 转发给本机后台；生产构建由后台直接托管 dist/。
const BACKEND = process.env.ORBIT8D_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": BACKEND },
  },
  build: {
    outDir: "dist",
    target: "es2022",
  },
  worker: {
    format: "es",
  },
  test: {
    include: ["src/**/*.test.ts"],
  },
});
