declare module "plotly.js-basic-dist-min" {
  interface PlotlyApi {
    react(element: HTMLElement, data: unknown[], layout: object, config: object): Promise<void>
    purge(element: HTMLElement): void
  }

  const Plotly: PlotlyApi
  export default Plotly
}
