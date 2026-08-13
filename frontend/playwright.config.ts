import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    baseURL: process.env.LABBRIDGE_DEMO_URL ?? "http://localhost:8000",
    browserName: "chromium",
    channel: "msedge",
    headless: true,
    trace: "on",
    screenshot: "only-on-failure",
  },
})
