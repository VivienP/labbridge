import { useState } from "react"

import type { SourceArtifactView } from "../workflow/model"

interface SourceStepProps {
  source: SourceArtifactView | null
  pending: boolean
  error?: string
  onLoadFixture: () => void
  onUpload: (
    file: File,
    dataOrigin: "observed" | "synthetic",
    executionMode: "replay" | "simulation" | "live",
  ) => void
}

export function SourceStep({
  source,
  pending,
  error,
  onLoadFixture,
  onUpload,
}: SourceStepProps) {
  const [file, setFile] = useState<File | null>(null)
  const [origin, setOrigin] = useState<"" | "observed" | "synthetic">("")
  const [mode, setMode] = useState<"" | "replay" | "simulation" | "live">("")
  return (
    <section className="step" aria-labelledby="source-title">
      <div className="step-number">01</div>
      <div className="step-content">
        <p className="eyebrow">Exact source bytes</p>
        <h2 id="source-title">Retain one CV source</h2>
        <p className="lede">
          Start with the committed synthetic fixture or explicitly classify and upload one CSV.
        </p>
        <div className="source-actions">
          <button type="button" className="primary" onClick={onLoadFixture} disabled={pending}>
            {pending ? "Retaining source…" : "Load synthetic fixture"}
          </button>
          <span className="or">or</span>
          <div className="upload-controls">
            <label>
              CV CSV
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
              />
            </label>
            <label>
              Data origin
              <select
                value={origin}
                onChange={(event) => setOrigin(event.currentTarget.value as typeof origin)}
              >
                <option value="">Select origin</option>
                <option value="synthetic">Synthetic</option>
                <option value="observed">Observed</option>
              </select>
            </label>
            <label>
              Execution mode
              <select
                value={mode}
                onChange={(event) => setMode(event.currentTarget.value as typeof mode)}
              >
                <option value="">Select mode</option>
                <option value="replay">Replay</option>
                <option value="simulation">Simulation</option>
                <option value="live">Live</option>
              </select>
            </label>
            <button
              type="button"
              onClick={() => file && origin && mode && onUpload(file, origin, mode)}
              disabled={pending || file === null || origin === "" || mode === ""}
            >
              Upload classified CSV
            </button>
          </div>
        </div>
        {error && <p role="alert" className="error">{error}</p>}
        {source && (
          <article className="result-card source-card">
            {source.data_origin === "synthetic" && (
              <p className="synthetic-banner">Synthetic data — not measured</p>
            )}
            <dl className="identity-grid">
              <div><dt>Filename</dt><dd>{source.filename}</dd></div>
              <div><dt>Size</dt><dd>{source.byte_size.toLocaleString()} bytes</dd></div>
              <div><dt>Origin + mode</dt><dd>{source.data_origin} + {source.execution_mode}</dd></div>
              <div><dt>State</dt><dd>{source.state}</dd></div>
              <div className="wide"><dt>SHA-256</dt><dd><code>{source.sha256}</code></dd></div>
              <div className="wide"><dt>Source identity</dt><dd><code>{source.source_artifact_id}</code></dd></div>
            </dl>
          </article>
        )}
      </div>
    </section>
  )
}
