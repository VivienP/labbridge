import type { ReactNode } from "react"

interface PanelProps {
  children: ReactNode
  title?: ReactNode
  description?: ReactNode
  aside?: ReactNode
  tone?: "default" | "sunken" | "accent"
  className?: string
}

/** A titled surface. Panels group one idea; stages group panels. */
export function Panel({
  children,
  title,
  description,
  aside,
  tone = "default",
  className,
}: PanelProps) {
  return (
    <section className={className ? `panel ${className}` : "panel"} data-tone={tone}>
      {(title !== undefined || aside !== undefined) && (
        <header className="panel-head">
          <div className="panel-heading">
            {title !== undefined && <h3>{title}</h3>}
            {description !== undefined && <p className="panel-description">{description}</p>}
          </div>
          {aside !== undefined && <div className="panel-aside">{aside}</div>}
        </header>
      )}
      <div className="panel-body">{children}</div>
    </section>
  )
}
