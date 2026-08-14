import type { ReactNode } from "react"

export interface KeyValueItem {
  label: string
  value: ReactNode
  wide?: boolean
}

interface KeyValueProps {
  items: KeyValueItem[]
  columns?: 2 | 3 | 4
  className?: string
}

/** A dense definition grid for identity and classification fields. */
export function KeyValue({ items, columns = 3, className }: KeyValueProps) {
  return (
    <dl className={className ? `kv ${className}` : "kv"} data-columns={columns}>
      {items.map((item) => (
        <div className={item.wide ? "kv-item kv-item-wide" : "kv-item"} key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}
