import { execFileSync } from "node:child_process"
import { mkdir, writeFile } from "node:fs/promises"
import path from "node:path"

import { expect, test, type Page } from "@playwright/test"

const evidenceDirectory = path.resolve("test-results", "evidence")
const appContainer = process.env.LABBRIDGE_DEMO_CONTAINER ?? "labbridge-cv-passport-demo-app-1"

function runContainerCli<T>(args: string[]): T {
  return JSON.parse(
    execFileSync("docker", ["exec", appContainer, "labbridge", ...args, "--json"], {
      encoding: "utf8",
    }),
  ) as T
}

function luminance([red, green, blue]: [number, number, number]): number {
  const channels = [red, green, blue].map((value) => {
    const component = value / 255
    return component <= 0.03928 ? component / 12.92 : ((component + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * channels[0]! + 0.7152 * channels[1]! + 0.0722 * channels[2]!
}

function rgb(value: string): [number, number, number] {
  const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/)
  if (!match) throw new Error(`unsupported computed colour: ${value}`)
  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

function contrastRatio(foreground: string, background: string): number {
  const [lighter, darker] = [luminance(rgb(foreground)), luminance(rgb(background))].sort(
    (left, right) => right - left,
  )
  return (lighter! + 0.05) / (darker! + 0.05)
}

async function chooseExplicitMapping(page: Page) {
  const mapping = page.getByRole("group", { name: "Column mapping decisions" })
  await mapping.getByLabel("Role for sample_index").selectOption("ignored")
  await mapping.getByLabel("Role for channel_a").selectOption("potential")
  await mapping.getByLabel("Source unit for channel_a").fill("V")
  await mapping.getByLabel("Target unit for channel_a").fill("V")
  await mapping.getByLabel("Role for channel_b").selectOption("current")
  await mapping.getByLabel("Source unit for channel_b").fill("A")
  await mapping.getByLabel("Target unit for channel_b").fill("A")
}

test("synthetic bytes reach a superseding Passport and CLI-verified Package", async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const remoteRequests: string[] = []
  page.on("request", (request) => {
    const url = new URL(request.url())
    if (!(["localhost", "127.0.0.1"].includes(url.hostname) || url.protocol === "data:")) {
      remoteRequests.push(request.url())
    }
  })

  await page.goto("/")
  await expect(page).toHaveTitle("LabBridge — CV Passport")
  await expect(page.getByText(/classification awaits human electrochemistry domain review/i)).toBeVisible()
  await page.keyboard.press("Tab")
  const focused = page.getByRole("button", { name: "Load synthetic fixture" })
  await expect(focused).toBeFocused()
  expect(await focused.evaluate((element) => getComputedStyle(element).outlineWidth)).not.toBe("0px")
  const loadFixtureButton = page.locator(".source-actions > button").first()
  const heroColours = await page.locator(".hero").evaluate((element) => {
    const style = getComputedStyle(element)
    return { foreground: style.color, background: style.backgroundColor }
  })
  expect(contrastRatio(heroColours.foreground, heroColours.background)).toBeGreaterThanOrEqual(4.5)

  let continueSourceRequest: (() => void) | undefined
  const sourceRequestGate = new Promise<void>((resolve) => { continueSourceRequest = resolve })
  await page.route("**/source-artifacts?**", async (route) => {
    await sourceRequestGate
    await route.continue()
  }, { times: 1 })
  await loadFixtureButton.click()
  await expect(loadFixtureButton).toHaveText("Retaining source…")
  continueSourceRequest?.()
  await expect(page.getByText("synthetic-cv-passport-demo.csv")).toBeVisible()
  const retainedSource = page.locator(".source-card code").last()
  const sourceIdentity = await retainedSource.textContent()
  expect(sourceIdentity).toMatch(/^source:/)

  await page.getByRole("button", { name: "Normalise explicit mapping" }).click()
  await expect(page.getByRole("alert")).toContainText("every inspected column requires")
  await chooseExplicitMapping(page)

  await page.route("**/cv/import-profiles", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "demo_profile_unavailable", message: "Retry this mapping." } }),
    })
  }, { times: 1 })
  await page.getByRole("button", { name: "Normalise explicit mapping" }).click()
  await expect(page.getByRole("alert")).toContainText("demo_profile_unavailable")
  await expect(retainedSource).toHaveText(sourceIdentity!)

  const experimentResponse = page.waitForResponse(
    (response) => response.request().method() === "POST" && /\/experiments$/.test(response.url()),
  )
  await page.getByRole("button", { name: "Normalise explicit mapping" }).click()
  const storedExperiment = await (await experimentResponse).json() as {
    experiment: { experiment_id: string }
  }
  await expect(page.getByRole("heading", { name: "Synthetic normalised CV trace" })).toBeVisible()
  await expect(page.getByRole("heading", { name: "Metadata, provenance, and findings" })).toBeVisible()
  await expect(page.getByText("Scientific validity")).toBeVisible()
  await expect(page.getByText("Not established by this release decision")).toBeVisible()
  await expect(page.locator(".finding").filter({ hasText: "metadata.reference_scale.unknown" }))
    .toContainText("reference_scale remains unknown")

  await page.getByRole("button", { name: "Release initial Passport" }).click()
  await expect(page.getByText("Synthetic Experiment Passport")).toBeVisible()
  const initialPassport = await page.locator(".passport-card code").first().textContent()
  expect(initialPassport).toMatch(/^passport:/)
  await expect(page.getByText(/does not infer or validate this reference scale as physically correct/i)).toBeVisible()

  await page.route("**/experiments/*/assertions", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "experiment_version_conflict", message: "Refresh and retry." } }),
    })
  }, { times: 1 })
  await page.getByRole("button", { name: "Add user-supplied RHE declaration" }).click()
  await expect(page.getByRole("alert")).toContainText("experiment_version_conflict")
  await expect(retainedSource).toHaveText(sourceIdentity!)

  await page.getByRole("button", { name: "Add user-supplied RHE declaration" }).click()
  await expect(page.getByText("Passport preview")).toBeVisible()
  const referenceRow = page.getByRole("row", { name: /reference_scale RHE user_supplied/ })
  await expect(referenceRow).toContainText("RHE")
  await expect(referenceRow).toContainText("user_supplied")

  await page.getByRole("button", { name: "Release superseding Passport" }).click()
  await expect(page.getByText("Superseding immutable release")).toBeVisible()
  await expect(page.getByText(`Supersedes ${initialPassport}`)).toBeVisible()
  await page.getByRole("button", { name: "Create Experiment Package" }).click()
  await expect(page.getByText("Synthetic Experiment Package")).toBeVisible()
  const packageIdentity = await page.locator(".package-card dd code").first().textContent()
  expect(packageIdentity).toMatch(/^experiment-package:/)
  const visibleFindingIds = await page.locator(".finding-id").allTextContents()
  const cliValidation = runContainerCli<{
    validation: { findings: Array<{ finding_id: string }> }
  }>([
    "experiment", "validate", storedExperiment.experiment.experiment_id,
    "--expected-version", "2", "--idempotency-key", "cli-parity-validation",
  ])
  const cliPassport = runContainerCli<{
    passport: { passport_id: string }
  }>([
    "experiment", "passport-release", storedExperiment.experiment.experiment_id,
    "--expected-version", "2", "--idempotency-key", "cli-parity-passport",
  ])
  const cliPackage = runContainerCli<{
    package: { package_id: string; archive_sha256: string }
  }>([
    "package", "create", storedExperiment.experiment.experiment_id,
    "--passport-id", cliPassport.passport.passport_id,
    "--expected-version", "2", "--idempotency-key", "cli-parity-package",
  ])
  const uiPackageChecksum = await page.locator(".package-card dd code").nth(2).textContent()
  expect(cliValidation.validation.findings.map((finding) => finding.finding_id)).toEqual(visibleFindingIds)
  expect(cliPassport.passport.passport_id).toBe(await page.locator(".passport-card code").last().textContent())
  expect(cliPackage.package.package_id).toBe(packageIdentity)
  expect(cliPackage.package.archive_sha256).toBe(uiPackageChecksum)

  const downloadPromise = page.waitForEvent("download")
  await page.getByRole("button", { name: "Download exact ZIP" }).click()
  const download = await downloadPromise
  await mkdir(evidenceDirectory, { recursive: true })
  const archivePath = path.join(evidenceDirectory, "synthetic-experiment-package.zip")
  await download.saveAs(archivePath)

  const containerArchive = "/tmp/synthetic-experiment-package.zip"
  execFileSync("docker", ["cp", archivePath, `${appContainer}:${containerArchive}`])
  const verificationText = execFileSync(
    "docker",
    ["exec", appContainer, "labbridge", "package", "verify", containerArchive, "--json"],
    { encoding: "utf8" },
  )
  const verification = JSON.parse(verificationText) as { package_id: string; verified: boolean }
  expect(verification.package_id).toBe(packageIdentity)
  expect(verification.verified).toBe(true)
  const verificationPath = path.join(evidenceDirectory, "cli-verification.json")
  await writeFile(verificationPath, `${JSON.stringify(verification, null, 2)}\n`, "utf8")

  const screenshotPath = path.join(evidenceDirectory, "final-package.png")
  await page.screenshot({ path: screenshotPath, fullPage: true })
  await testInfo.attach("verified-package", { path: archivePath, contentType: "application/zip" })
  await testInfo.attach("cli-verification", { path: verificationPath, contentType: "application/json" })
  await testInfo.attach("final-package", { path: screenshotPath, contentType: "image/png" })
  expect(remoteRequests).toEqual([])
})
