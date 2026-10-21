import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEV_PORT = Number(process.env.ZOOAGENT_DEV_PORT ?? 7801);
const DEV_TOKEN = process.env.ZOOAGENT_DEV_TOKEN ?? "dev-token";

/**
 * Starts the local Python service together with the dev server, so
 * `pnpm dev` (and therefore `pnpm tauri dev`) is a single command. In
 * production the Tauri shell spawns the packaged sidecar instead.
 */
function backendPlugin(): Plugin {
  let child: ChildProcess | null = null;
  return {
    name: "zooagent-backend",
    apply: "serve",
    configureServer(server) {
      const repoRoot = path.resolve(__dirname, "../..");
      const workspace = process.env.ZOOAGENT_HOME ?? path.join(repoRoot, ".workspace");
      server.config.logger.info(
        `[zooagent] 启动后端 http://127.0.0.1:${DEV_PORT} (workspace: ${workspace})`,
      );
      child = spawn(
        "uv",
        [
          "run",
          "zooagent",
          "serve",
          "--port",
          String(DEV_PORT),
          "--token",
          DEV_TOKEN,
          "--workspace",
          workspace,
        ],
        { cwd: path.join(repoRoot, "backend"), stdio: "inherit" },
      );

      const shutdown = () => {
        child?.kill();
        child = null;
      };
      server.httpServer?.on("close", shutdown);
      process.on("exit", shutdown);
      process.on("SIGINT", shutdown);
      process.on("SIGTERM", shutdown);
    },
  };
}

export default defineConfig({
  plugins: [react(), backendPlugin()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${DEV_PORT}`,
        changeOrigin: true,
        headers: { Authorization: `Bearer ${DEV_TOKEN}` },
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
