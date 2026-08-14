import { useState } from "react"

import type { SourceArtifactView } from "../workflow/model"
import { Badge } from "./ui/Badge"
import { Callout } from "./ui/Callout"
import { KeyValue } from "./ui/KeyValue"
import { Panel } from "./ui/Panel"

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
  const uploadReady = file !== null && origin !== "" && mode !== ""
  return (
    <>
      <div className="source-actions">
        <Panel
          title="Committed synthetic fixture"
          description="Classified synthetic + replay by the demo, not inferred from the bytes."
        >
          <button
            type="button"
            className="primary"
            onClick={onLoadFixture}
            disabled={pending}
            aria-busy={pending}
          >
            {pending && <span className="spinner" aria-hidden="true" />}
            Load synthetic fixture
          </button>
          <p className="action-note">
            Retains the committed fixture byte-for-byte and reads back its headers. No column
            meaning is assigned here.
          </p>
        </Panel>
        <Panel
          title="Your own CV CSV"
          description="LabBridge never infers where a file came from or how it was produced."
        >
          <div className="upload-controls">
            <label className="field field-wide">
              <span className="field-label">CV CSV</span>
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
              />
            </label>
            <label className="field">
              <span className="field-label">Data origin</span>
              <select
                value={origin}
                onChange={(event) => setOrigin(event.currentTarget.value as typeof origin)}
              >
                <option value="">Select origin</option>
                <option value="synthetic">Synthetic</option>
                <option value="observed">Observed</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">Execution mode</span>
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
          </div>
          <button
            type="button"
            onClick={() => file && origin && mode && onUpload(file, origin, mode)}
            disabled={pending || !uploadReady}
          >
            Upload classified CSV
          </button>
          <p className="action-note">
            {uploadReady
              ? "Both classifications are your declaration and are retained with the bytes."
              : "Choose a file, a data origin, and an execution mode to enable the upload."}
          </p>
        </Panel>
      </div>
      {pending && (
        <p className="working-note" role="status">
          Retaining source bytes…
        </p>
      )}
      {error && (
        <Callout tone="blocking" role="alert" title="Source intake failed">
          <p className="error-message">{error}</p>
        </Callout>
      )}
      {source && (
        <article className="result-card">
          <div className="result-head">
            <h3>Retained source artifact</h3>
            {source.data_origin === "synthetic" && (
              <p className="synthetic-banner">Synthetic data — not measured</p>
            )}
          </div>
          <KeyValue
            columns={4}
            items={[
              { label: "Filename", value: source.filename },
              { label: "Size", value: `${source.byte_size.toLocaleString()} bytes` },
              {
                label: "Declared data origin",
                value: (
                  <Badge tone={source.data_origin === "synthetic" ? "attention" : "accent"}>
                    {source.data_origin}
                  </Badge>
                ),
              },
              {
                label: "Declared execution mode",
                value: <Badge tone="neutral">{source.execution_mode}</Badge>,
              },
              { label: "State", value: source.state, wide: true },
              { label: "Media type", value: source.media_type, wide: true },
              { label: "SHA-256", value: <code>{source.sha256}</code>, wide: true },
              {
                label: "Source identity",
                value: (
                  <code data-identity="source-artifact">{source.source_artifact_id}</code>
                ),
                wide: true,
              },
            ]}
          />
          <p className="result-note">
            The checksum and identity are derived from the exact bytes. Nothing above interprets the
            file as electrochemistry.
          </p>
        </article>
      )}
    </>
  )
}
