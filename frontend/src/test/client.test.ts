import { describe, expect, it, vi } from "vitest"

import { ApiRequestError, createApiClient } from "../api/client"

describe("typed API client", () => {
  it("decodes the stable backend code without parsing message prose", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: { code: "experiment_version_conflict", message: "The prose may change." },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    )
    const api = createApiClient(fetcher)

    await expect(api.getExperiment("experiment:one")).rejects.toEqual(
      new ApiRequestError(409, "experiment_version_conflict", "The prose may change."),
    )
  })

  it("submits fixture bytes with explicit origin and mode", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json({
        source_artifact_id: "source:one",
        filename: "synthetic-demo.csv",
        media_type: "text/csv",
        byte_size: 4,
        sha256: "a".repeat(64),
        data_origin: "synthetic",
        execution_mode: "replay",
        state: "committed",
        object_uri: "s3://labbridge/source",
        replayed: false,
      }),
    )
    const bytes = new Blob(["x,y\n"], { type: "text/csv" })
    const api = createApiClient(fetcher)

    await api.intakeSource(bytes, {
      filename: "synthetic-demo.csv",
      dataOrigin: "synthetic",
      executionMode: "replay",
      idempotencyKey: "source-intent",
    })

    const [url, request] = fetcher.mock.calls[0] ?? []
    expect(String(url)).toBe(
      "/source-artifacts?filename=synthetic-demo.csv&data_origin=synthetic&execution_mode=replay",
    )
    expect(request?.body).toBe(bytes)
    expect(new Headers(request?.headers).get("Idempotency-Key")).toBe("source-intent")
  })

  it("returns downloaded Package bytes without decoding them", async () => {
    const bytes = new Uint8Array([80, 75, 3, 4])
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(bytes, { headers: { "Content-Type": "application/zip" } }),
    )
    const api = createApiClient(fetcher)

    const downloaded = await api.downloadPackage("package:one")

    expect(new Uint8Array(await downloaded.arrayBuffer())).toEqual(bytes)
  })
})
