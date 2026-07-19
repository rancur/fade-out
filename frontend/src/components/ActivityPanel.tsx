import { useState } from 'react'
import clsx from 'clsx'
import { Activity, ChevronRight, AlertTriangle, AlertCircle, Info } from 'lucide-react'
import { useActivity, type ActivityItem } from '../api/hooks'

const LEVELS = ['all', 'info', 'warn', 'error'] as const
type Level = (typeof LEVELS)[number]

const levelStyle: Record<string, { text: string; dot: string; icon: typeof Info }> = {
  info: { text: 'text-accent', dot: 'bg-accent', icon: Info },
  warn: { text: 'text-gold', dot: 'bg-gold', icon: AlertTriangle },
  error: { text: 'text-cyber-red', dot: 'bg-cyber-red', icon: AlertCircle },
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
    <div className="flex gap-2 px-3 py-2 border-b border-white/5 hover:bg-white/5 animate-slide-in">
      <Icon className={clsx('w-3.5 h-3.5 mt-0.5 shrink-0', style.text)} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-gray-500">{fmtTime(item.ts)}</span>
          {item.event && (
            <span className={clsx('text-[10px] font-mono truncate', style.text)}>{item.event}</span>
          )}
        </div>
        <p className="text-xs text-gray-300 break-words leading-snug mt-0.5">{item.message}</p>
        {(item.filename || item.platform || item.stage) && (
          <p className="text-[10px] text-gray-600 font-mono truncate mt-0.5">
            {[item.stage, item.platform, item.filename].filter(Boolean).join(' · ')}
          </p>
        )}
      </div>
    </div>
  )
}

export default function ActivityPanel() {
  const [collapsed, setCollapsed] = useState(false)
  const [level, setLevel] = useState<Level>('all')
  const [mixFilter, setMixFilter] = useState('')

  const { data, isLoading, isError } = useActivity({
    level: level === 'all' ? undefined : level,
    mix_id: mixFilter.trim() || undefined,
  })

  if (collapsed) {
    return (
      <button
        onClick={() => setCollapsed(false)}
        title="Show activity log"
        className="flex flex-col items-center gap-2 w-10 shrink-0 border-l border-primary/20 bg-surface-light text-gray-400 hover:text-primary py-3"
      >
        <Activity className="w-5 h-5" />
        <span className="text-[10px] font-mono [writing-mode:vertical-rl] rotate-180 tracking-wider">
          ACTIVITY
        </span>
      </button>
    )
  }

  return (
    <aside className="flex flex-col w-80 shrink-0 border-l border-primary/20 bg-surface-light z-10">
      <div className="flex items-center gap-2 px-3 h-16 border-b border-primary/10">
        <Activity className="w-5 h-5 text-primary shrink-0" />
        <div className="flex-1">
          <h2 className="font-pixel text-[10px] text-primary tracking-wider">activity</h2>
          <p className="text-[10px] text-gray-500 font-mono">live event log</p>
        </div>
        <button
          onClick={() => setCollapsed(true)}
          title="Collapse"
          className="text-gray-500 hover:text-primary"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>

      <div className="flex flex-col gap-2 px-3 py-2 border-b border-primary/10">
        <div className="flex gap-1">
          {LEVELS.map((l) => (
            <button
              key={l}
              onClick={() => setLevel(l)}
              className={clsx(
                'px-2 py-1 rounded text-[10px] font-mono uppercase transition-colors',
                level === l
                  ? 'bg-primary/20 text-primary'
                  : 'text-gray-500 hover:text-gray-300 hover:bg-white/5',
              )}
            >
              {l}
            </button>
          ))}
        </div>
        <input
          value={mixFilter}
          onChange={(e) => setMixFilter(e.target.value)}
          placeholder="filter by mix id…"
          className="w-full bg-surface border border-white/10 rounded px-2 py-1 text-xs font-mono text-gray-300 placeholder:text-gray-600 focus:outline-none focus:border-primary/40"
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {isLoading && <p className="p-3 text-xs text-gray-500 font-mono">Loading…</p>}
        {isError && (
          <p className="p-3 text-xs text-cyber-red font-mono">Failed to load activity.</p>
        )}
        {data && data.items.length === 0 && (
          <p className="p-3 text-xs text-gray-500 font-mono">No activity yet.</p>
        )}
        {data?.items.map((item) => <Row key={item.id} item={item} />)}
      </div>

      {data && (
        <div className="px-3 py-1.5 border-t border-primary/10 text-[10px] text-gray-600 font-mono">
          {data.total} events · refreshing 4s
        </div>
      )}
    </aside>
  )
}
