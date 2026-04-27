import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    testTimeout: 30_000,
    hookTimeout: 10_000,
    reporters: ["verbose"],
    // Sequential execution — tests share a single gateway instance
    pool: "forks",
    poolOptions: {
      forks: { singleFork: true },
    },
  },
});
