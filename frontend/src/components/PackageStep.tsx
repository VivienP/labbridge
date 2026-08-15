import type { PackageView } from "../workflow/model"
import { Badge } from "./ui/Badge"
import { Callout } from "./ui/Callout"
import { KeyValue } from "./ui/KeyValue"

interface CustodyChain {
  sourceArtifactId: string
  importProfileId: string
  observationId: string
}

interface PackageStepProps {
  packageView: PackageView | null
  pending: boolean
  error?: string
  chain?: CustodyChain
  onCreate: () => void
  onDownload: () => void
}

export function PackageStep({
  packageView,
  pending,
  error,
  chain,
  onCreate,
  onDownload,
}: PackageStepProps) {
  const producing = packageView ? Object.entries(packageView.package.producing_versions ?? {}) : []
  return (
    <>
      {!packageView ? (
        <div className="action-row">
          <button
            type="button"
            className="primary"
            disabled={pending}
            aria-busy={pending}
            onClick={onCreate}
          >
            {pending && <span className="spinner" aria-hidden="true" />}
            Create Experiment Package
          </button>
          <p className="action-note">
            Assembles the retained source bytes, the import profile, the normalised observation, the
            transformation graph, and the released Passport into one checksummed archive with a
            closed lineage record. The archive is immutable once created.
          </p>
        </div>
      ) : (
        <article className="result-card package-card">
          <div className="result-head">
            <h3>
              {packageView.package.data_origin === "synthetic" ? "Synthetic" : "Observed"} Experiment
              Package
            </h3>
            {packageView.package.data_origin === "synthetic" && (
              <p className="synthetic-banner">Synthetic archive — not measured</p>
            )}
          </div>
          <KeyValue
            columns={3}
            items={[
              {
                label: "Package",
                value: <code data-identity="package">{packageView.package.package_id}</code>,
                wide: true,
              },
              {
                label: "Passport",
                value: <code data-identity="package-passport">{packageView.package.passport_id}</code>,
                wide: true,
              },
              {
                label: "Archive SHA-256",
                value: (
                  <code data-identity="archive-sha256">{packageView.package.archive_sha256}</code>
                ),
                wide: true,
              },
              {
                label: "Size",
                value: `${packageView.package.archive_byte_size.toLocaleString()} bytes`,
              },
              {
                label: "Data origin",
                value: (
                  <Badge tone={packageView.package.data_origin === "synthetic" ? "attention" : "accent"}>
                    {packageView.package.data_origin}
                  </Badge>
                ),
              },
              {
                label: "Execution mode",
                value: <Badge tone="neutral">{packageView.package.execution_mode}</Badge>,
              },
            ]}
          />
          {chain && (
            <section className="custody" aria-labelledby="custody-title">
              <h4 id="custody-title">Chain of custody closed by this Package</h4>
              <ol className="custody-chain">
                <li>
                  <span className="custody-label">Source bytes</span>
                  <code>{chain.sourceArtifactId}</code>
                </li>
                <li>
                  <span className="custody-label">Import profile</span>
                  <code>{chain.importProfileId}</code>
                </li>
                <li>
                  <span className="custody-label">Normalised observation</span>
                  <code>{chain.observationId}</code>
                </li>
                <li>
                  <span className="custody-label">
                    Experiment version {packageView.package.experiment_version}
                  </span>
                  <code>{packageView.package.experiment_id}</code>
                </li>
                <li>
                  <span className="custody-label">Released Passport</span>
                  <code>{packageView.package.passport_id}</code>
                </li>
              </ol>
            </section>
          )}
          {producing.length > 0 && (
            <details className="disclosure">
              <summary>
                Producing versions
                <span className="disclosure-count">{producing.length}</span>
              </summary>
              <ul className="producing-versions">
                {producing.map(([component, version]) => (
                  <li key={component}>
                    <span className="parameter-name">{component}</span>
                    <code>{version}</code>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <Callout tone="attention" title="Released; CLI verification required">
            <p>
              The browser download is a transfer, not a verification. Recompute the archive checksum
              and lineage outside this page:
            </p>
            <pre className="command">labbridge package verify &lt;archive.zip&gt;</pre>
          </Callout>
          <div className="action-row">
            <button
              type="button"
              className="primary"
              disabled={pending}
              aria-busy={pending}
              onClick={onDownload}
            >
              {pending && <span className="spinner" aria-hidden="true" />}
              Download exact ZIP
            </button>
            <p className="action-note">
              Streams the stored archive bytes. Downloading creates nothing and changes nothing.
            </p>
          </div>
        </article>
      )}
      {error && (
        <Callout tone="blocking" role="alert" title="Package step failed">
          <p className="error-message">{error}</p>
        </Callout>
      )}
    </>
  )
}
