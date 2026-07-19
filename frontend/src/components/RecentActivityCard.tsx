import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { Activity, AlertTriangle, AlertCircle, Info, ArrowRight } from 'lucide-react'
import { useActivity, type ActivityItem } from '../api/hooks'

const levelStyle: Record<string, { text: string; icon: typeof Info }> = {
  info: { text: 'text-accent', icon: Info },
  warn: { text: 'text-gold', icon: AlertTriangle },
  error: { text: 'text-cyber-red', icon: AlertCircle },
}

function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function Row({ item }: { item: ActivityItem }) {
  const style = levelStyle[item.level] ?? levelStyle.info
  const Icon = style.icon
  return (
    <div className="flex gap-2 px-5 py-2.5 border-b border-white/5 hover:bg-white/[0.02]">
      <Icon className={clsx('w-3.5 h-3.5 mt-0.5 shrink-0', style.text)} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-gray-500">{fmtTime(item.ts)}</span>
          {item.event && (
            <span className={clsx('text-[10px] font-mono truncate', style.text)}>{item.event}</span>
          )}
        </div>
        <p className="text-xs text-gray-300 break-words leading-snug mt-0.5">{item.message}</p>
      </div>
    </div>
  )
}

/** Compact "recent activity" widget for the Dashboard; links to /activity. */
export default function RecentActivityCard() {
  const { data, isLoading, isError } = useActivity({ limit: 10 })
  const items = (data?.items ?? []).slice(0, 10)

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
      <div className="flex items-center justify-between px-5 py-4 border-b border-primary/10">
        <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
          <Activity className="w-4 h-4 text-primary" /> Recent Activity
        </h2>
        <Link
          to="/activity"
          className="text-[11px] text-primary hover:text-primary/80 flex items-center gap-1"
        >
          See all <ArrowRight className="w-3 h-3" />
        </Link>
      </div>
      <div className="max-h-80 overflow-y-auto">
        {isLoading && <p className="p-5 text-xs text-gray-500 font-mono">Loading…</p>}
        {isError && <p className="p-5 text-xs text-cyber-red font-mono">Failed to load activity.</p>}
        {data && items.length === 0 && (
          <p className="p-5 text-xs text-gray-500 font-mono">No activity yet.</p>
        )}
        {items.map((item) => (
          <Row key={item.id} item={item} />
        ))}
      </div>
    </div>
  )
}
