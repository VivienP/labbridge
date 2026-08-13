import type { NormalisationView, PlotSeriesView } from "../workflow/model"
import { Plot } from "./Plot"

interface ObservationStepProps {
  normalisation: NormalisationView
  plot: PlotSeriesView
}

export function ObservationStep({ normalisation, plot }: ObservationStepProps) {
  const originLabel = plot.data_origin === "synthetic" ? "Synthetic" : "Observed"
  return (
    <section className="step" aria-labelledby="observation-title">
      <div className="step-number">03</div>
      <div className="step-content">
        <p className="eyebrow">Backend-approved values</p>
        <h2 id="observation-title">{originLabel} normalised CV trace</h2>
        {plot.data_origin === "synthetic" && (
          <p className="synthetic-banner">Synthetic data — illustrative, not measured</p>
        )}
        <Plot plot={plot} />
        <dl className="identity-grid compact">
          <div className="wide"><dt>Observation</dt><dd><code>{plot.observation_id}</code></dd></div>
          <div><dt>Rows</dt><dd>{normalisation.result.observation.row_count}</dd></div>
          <div><dt>Environment</dt><dd>{plot.environment_id}</dd></div>
          <div><dt>Origin</dt><dd>{plot.data_origin}</dd></div>
          <div><dt>Mode</dt><dd>{plot.execution_mode}</dd></div>
        </dl>
        <details>
          <summary>Transformation provenance</summary>
          <ol className="provenance-list">
            {normalisation.result.graph.records.map((record) => (
              <li key={record.transformation_id}>
                <strong>{record.kind}</strong> <code>{record.transformation_id}</code>
              </li>
            ))}
          </ol>
        </details>
        {normalisation.result.findings.length > 0 && (
          <ul className="findings structural">
            {normalisation.result.findings.map((finding) => (
              <li key={finding.finding_id}>{finding.status}: {finding.code} — {finding.message}</li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
