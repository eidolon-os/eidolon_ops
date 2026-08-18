import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands inside the Python package, which is what `eidolon-ops-console`
// serves. `npm run dev` proxies the API to a console already running on 9010, so
// the interface can be worked on without a second copy of the backend.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../src/eidolon_ops/console/static",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    port: 9011,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:9010",
        changeOrigin: true,
      },
    },
  },
});
