import { useEffect, useRef } from "react"
import Plotly from "plotly.js-basic-dist-min"

import type { PlotSeriesView } from "../workflow/model"

interface PlotProps {
  plot: PlotSeriesView
}

/** Mirrors the stylesheet tokens --accent, --ink-soft, --line and --surface-raised. */
const TRACE = "#0d5566"
const AXIS_INK = "#55636d"
const GRID = "#e4e9ee"
const ZERO_LINE = "#b6c0c9"
const PLOT_BACKGROUND = "#fbfcfd"
const PLOT_FONT =
  'Inter, "Segoe UI", ui-sans-serif, system-ui, Roboto, Helvetica, Arial, sans-serif'

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
          // `simplify: false` keeps every backend point drawn; Plotly otherwise drops collinear ones.
          line: { color: TRACE, width: 2, shape: "linear", simplify: false },
          marker: { color: TRACE, size: 5, line: { color: PLOT_BACKGROUND, width: 1 } },
          name: `${originLabel} CV`,
          // The hover text repeats the exact decimal strings the backend sent, never a float of them.
          customdata: potential.values.map((value, index) => [value, current.values[index] ?? ""]),
          hovertemplate:
            `%{customdata[0]} ${potential.unit}<br>%{customdata[1]} ${current.unit}<extra></extra>`,
        },
      ],
      {
        datarevision: plot.observation_id,
        paper_bgcolor: "transparent",
        plot_bgcolor: PLOT_BACKGROUND,
        margin: { l: 76, r: 20, t: 16, b: 60 },
        font: { family: PLOT_FONT, size: 12, color: AXIS_INK },
        hoverlabel: { font: { family: PLOT_FONT, size: 12 } },
        xaxis: {
          title: { text: `${potential.role} (${potential.unit})`, standoff: 14 },
          gridcolor: GRID,
          zerolinecolor: ZERO_LINE,
          zerolinewidth: 1,
          linecolor: ZERO_LINE,
          ticks: "outside",
          tickcolor: GRID,
          automargin: true,
        },
        yaxis: {
          title: { text: `${current.role} (${current.unit})`, standoff: 18 },
          gridcolor: GRID,
          zerolinecolor: ZERO_LINE,
          zerolinewidth: 1,
          linecolor: ZERO_LINE,
          ticks: "outside",
          tickcolor: GRID,
          automargin: true,
        },
        showlegend: false,
      },
      {
        responsive: true,
        scrollZoom: false,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d", "toggleSpikelines"],
      },
    )
    const element = host.current
    return () => Plotly.purge(element)
  }, [current, originLabel, plot.observation_id, potential])
  if (!potential || !current) {
    return (
      <p role="alert" className="error-message">
        Backend plot response has no potential/current pair.
      </p>
    )
  }
  return (
    <figure className="plot-figure">
      <div ref={host} className="plot" aria-label={`${originLabel} normalised CV trace plot`} />
      <figcaption className="plot-summary">
        {originLabel} ordered series <code>{potential.series_id}</code> ({potential.unit}) against{" "}
        <code>{current.series_id}</code> ({current.unit}); values are displayed in backend order.
      </figcaption>
    </figure>
  )
}
