import { useEffect, useRef } from "react"
import Plotly from "plotly.js-basic-dist-min"

import type { PlotSeriesView } from "../workflow/model"

interface PlotProps {
  plot: PlotSeriesView
}

export function Plot({ plot }: PlotProps) {
  const host = useRef<HTMLDivElement>(null)
  const potential = plot.series.find((series) => series.role === "potential")
  const current = plot.series.find(
    (series) => series.role === "current" || series.role === "current_density",
  )
  const originLabel = plot.data_origin === "synthetic" ? "Synthetic" : "Observed"
  useEffect(() => {
    if (!host.current || !potential || !current) return
    void Plotly.react(
      host.current,
      [
        {
          x: potential.values,
          y: current.values,
          type: "scatter",
          mode: "lines+markers",
          line: { color: "#087f72", width: 3 },
          marker: { color: "#fbaf3c", size: 6 },
          name: `${originLabel} CV`,
        },
      ],
      {
        datarevision: plot.observation_id,
        paper_bgcolor: "transparent",
        plot_bgcolor: "#f8fbfa",
        margin: { l: 70, r: 25, t: 30, b: 65 },
        xaxis: { title: `${potential.role} (${potential.unit})`, zerolinecolor: "#a8b8b5" },
        yaxis: { title: `${current.role} (${current.unit})`, zerolinecolor: "#a8b8b5" },
        showlegend: false,
      },
      { responsive: true, scrollZoom: false, displaylogo: false },
    )
    const element = host.current
    return () => Plotly.purge(element)
  }, [current, originLabel, plot.observation_id, potential])
  if (!potential || !current) {
    return <p role="alert">Backend plot response has no potential/current pair.</p>
  }
  return (
    <>
      <div ref={host} className="plot" aria-label={`${originLabel} normalised CV trace plot`} />
      <p className="plot-summary">
        {originLabel} ordered series <code>{potential.series_id}</code> ({potential.unit}) against{" "}
        <code>{current.series_id}</code> ({current.unit}); values are displayed in backend order.
      </p>
    </>
  )
}
