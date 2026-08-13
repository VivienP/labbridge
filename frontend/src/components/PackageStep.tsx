import type { PackageView } from "../workflow/model"

interface PackageStepProps {
  packageView: PackageView | null
  pending: boolean
  error?: string
  onCreate: () => void
  onDownload: () => void
}

export function PackageStep({ packageView, pending, error, onCreate, onDownload }: PackageStepProps) {
  return (
    <section className="step" aria-labelledby="package-title">
      <div className="step-number">06</div>
      <div className="step-content">
        <p className="eyebrow">Closed evidence archive</p>
        <h2 id="package-title">Experiment Package</h2>
        {!packageView ? (
          <button type="button" className="primary" disabled={pending} onClick={onCreate}>
            {pending ? "Creating Package…" : "Create Experiment Package"}
          </button>
        ) : (
          <article className="result-card package-card">
            <p className={packageView.package.data_origin === "synthetic" ? "synthetic-banner" : undefined}>
              {packageView.package.data_origin === "synthetic" ? "Synthetic" : "Observed"} Experiment Package
            </p>
            <dl className="identity-grid compact">
              <div className="wide"><dt>Package</dt><dd><code>{packageView.package.package_id}</code></dd></div>
              <div className="wide"><dt>Passport</dt><dd><code>{packageView.package.passport_id}</code></dd></div>
              <div className="wide"><dt>Archive SHA-256</dt><dd><code>{packageView.package.archive_sha256}</code></dd></div>
              <div><dt>Size</dt><dd>{packageView.package.archive_byte_size.toLocaleString()} bytes</dd></div>
              <div><dt>Origin + mode</dt><dd>{packageView.package.data_origin} + {packageView.package.execution_mode}</dd></div>
            </dl>
            <p className="verification-pending">Released; CLI verification required</p>
            <button type="button" className="primary" disabled={pending} onClick={onDownload}>
              Download exact ZIP
            </button>
          </article>
        )}
        {error && <p role="alert" className="error">{error}</p>}
      </div>
    </section>
  )
}
