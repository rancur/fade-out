import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import {
  Music2,
  Upload,
  Clock,
  CheckCircle2,
  AlertTriangle,
  Zap,
  ArrowRight,
  Activity,
  Pause,
} from 'lucide-react'
import { toast } from 'sonner'
import { useMixes, usePipelineStatus, useAIBudget } from '@/api/hooks'
import { wsManager, type WsMessage } from '@/api/ws'
import StatusBadge from '@/components/StatusBadge'
import RecentActivityCard from '@/components/RecentActivityCard'
import clsx from 'clsx'

function StatCard({
  icon: Icon,
  label,
  value,
  accent,
  sub,
}: {
  icon: React.ElementType
  label: string
  value: string | number
  accent: string
  sub?: string
}) {
  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-5 card-hover">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">{label}</p>
          <p className={clsx('text-2xl font-bold mt-1', accent)}>{value}</p>
          {sub && <p className="text-[11px] text-gray-600 mt-1">{sub}</p>}
        </div>
        <div className={clsx('w-10 h-10 rounded-lg flex items-center justify-center', `bg-current/10`)}>
          <Icon className={clsx('w-5 h-5', accent)} />
        </div>
      </div>
    </div>
  )
}

function basename(path: string): string {
  return path.split('/').pop() ?? path
}

const activeStatuses = new Set(['running', 'analyzing', 'generating', 'uploading_soundcloud', 'uploading_youtube', 'verifying'])

export default function Dashboard() {
  const { data: mixesData } = useMixes({ page_size: 5 })
  const { data: pipeline } = usePipelineStatus()
  const { data: budget } = useAIBudget()

  // Surface live pipeline errors as toasts, deduped by mix+step
  const seenErrors = useRef(new Set<string>())
  useEffect(() => {
    return wsManager.subscribe('error', (msg: WsMessage) => {
      const step = typeof msg.data?.step === 'string' ? msg.data.step : ''
      const key = `${msg.mix_id ?? ''}:${step}`
      if (seenErrors.current.has(key)) return
      seenErrors.current.add(key)
      const detail =
        typeof msg.data?.error === 'string'
          ? msg.data.error
          : typeof msg.data?.message === 'string'
            ? msg.data.message
            : 'Pipeline error'
      toast.error(step ? `${step}: ${detail}` : detail)
    })
  }, [])

  const mixes = mixesData?.items ?? []
  const total = mixesData?.total ?? 0
  const completed = mixes.filter((m) => m.pipeline_status === 'completed').length
  const processing = mixes.filter((m) => activeStatuses.has(m.pipeline_status)).length
  const failed = mixes.filter((m) => m.pipeline_status === 'failed').length

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-pixel text-lg text-primary glow-text">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-1">Mix automation control center</p>
        </div>
        <Link
          to="/mixes"
          className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 transition-all"
        >
          <Music2 className="w-4 h-4" />
          View All Mixes
        </Link>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={Music2} label="Total Mixes" value={total} accent="text-primary" />
        <StatCard icon={Upload} label="Completed" value={completed} accent="text-cyber-lime" />
        <StatCard
          icon={Clock}
          label="Processing"
          value={processing}
          accent="text-cyber-cyan"
          sub={pipeline?.active ? `${pipeline.active} active` : undefined}
        />
        <StatCard icon={AlertTriangle} label="Failed" value={failed} accent="text-cyber-red" />
      </div>

      {/* Budget bar */}
      {budget && (
        <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Zap className="w-4 h-4 text-gold" />
              <span className="text-sm font-mono text-gray-300">AI Budget</span>
            </div>
            <span className="text-sm font-mono text-gray-400">
              ${budget.spent_this_month.toFixed(2)} / ${budget.monthly_budget.toFixed(2)}
            </span>
          </div>
          <div className="h-3 bg-dark rounded-full overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full transition-all duration-700',
                budget.percentage_used > 90
                  ? 'bg-gradient-to-r from-cyber-red to-cyber-orange'
                  : budget.percentage_used > 70
                    ? 'bg-gradient-to-r from-gold to-cyber-orange'
                    : 'bg-gradient-to-r from-primary to-cyber-cyan',
              )}
              style={{ width: `${Math.min(100, budget.percentage_used)}%` }}
            />
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Mixes */}
        <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-primary/10">
            <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
              <Music2 className="w-4 h-4 text-primary" /> Recent Mixes
            </h2>
            <Link to="/mixes" className="text-[11px] text-primary hover:text-primary/80 flex items-center gap-1">
              See all <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="divide-y divide-white/5">
            {mixes.length === 0 ? (
              <div className="px-5 py-8 text-center text-gray-600 text-sm">
                No mixes yet. Drop an audio file to get started.
              </div>
            ) : (
              mixes.map((mix) => (
                <Link
                  key={mix.id}
                  to={`/mixes/${mix.id}`}
                  className="flex items-center justify-between px-5 py-3 hover:bg-white/[0.02] transition-colors"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="w-8 h-8 rounded bg-primary/10 flex items-center justify-center shrink-0">
                      <Music2 className="w-4 h-4 text-primary/50" />
                    </div>
                    <div className="min-w-0">
                      <p className="text-sm text-gray-200 truncate">{mix.title}</p>
                      <p className="text-[10px] text-gray-600 font-mono">
                        {mix.audio_file_path ? basename(mix.audio_file_path) : 'No file'}
                      </p>
                    </div>
                  </div>
                  <StatusBadge status={mix.pipeline_status} />
                </Link>
              ))
            )}
          </div>
        </div>

        {/* Pipeline Status */}
        <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-primary/10">
            <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
              <Activity className="w-4 h-4 text-cyber-cyan" /> Pipeline
            </h2>
            <span className="text-[11px] font-mono text-gray-500">
              {pipeline?.queued ?? 0} queued
            </span>
          </div>
          <div className="p-5 space-y-4">
            {pipeline?.paused ? (
              <div className="flex items-center gap-3 p-3 bg-gold/5 border border-gold/20 rounded-lg">
                <Pause className="w-4 h-4 text-gold" />
                <p className="text-sm text-gold font-mono">Pipeline Paused</p>
              </div>
            ) : pipeline?.active ? (
              <div className="flex items-center gap-3 p-3 bg-cyber-cyan/5 border border-cyber-cyan/20 rounded-lg">
                <div className="w-3 h-3 rounded-full bg-cyber-cyan animate-pulse" />
                <div>
                  <p className="text-sm text-cyber-cyan font-mono">Currently Processing</p>
                  <p className="text-[11px] text-gray-500">
                    {pipeline.active} active &middot; {pipeline.queued} queued
                  </p>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-3 p-3 bg-white/[0.02] rounded-lg">
                <CheckCircle2 className="w-4 h-4 text-primary/50" />
                <p className="text-sm text-gray-500">Pipeline idle</p>
              </div>
            )}

            {/* Summary stats */}
            {pipeline && (
              <div className="grid grid-cols-2 gap-3">
                <div className="text-center p-2 bg-white/[0.02] rounded-lg">
                  <p className="text-lg font-bold text-cyber-lime">{pipeline.completed}</p>
                  <p className="text-[10px] text-gray-600 font-mono uppercase">Completed</p>
                </div>
                <div className="text-center p-2 bg-white/[0.02] rounded-lg">
                  <p className="text-lg font-bold text-cyber-red">{pipeline.failed}</p>
                  <p className="text-[10px] text-gray-600 font-mono uppercase">Failed</p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Recent Activity */}
      <RecentActivityCard />

      {/* Quick Actions */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { to: '/mixes', label: 'Browse Mixes', icon: Music2, color: 'text-primary border-primary/20 hover:border-primary/40' },
          { to: '/brand', label: 'Edit Brand', icon: Zap, color: 'text-secondary border-secondary/20 hover:border-secondary/40' },
          { to: '/ai', label: 'AI Costs', icon: Activity, color: 'text-accent border-accent/20 hover:border-accent/40' },
          { to: '/settings', label: 'Settings', icon: CheckCircle2, color: 'text-gold border-gold/20 hover:border-gold/40' },
        ].map(({ to, label, icon: Icon, color }) => (
          <Link
            key={to}
            to={to}
            className={clsx(
              'flex items-center gap-2 px-4 py-3 bg-surface-light border rounded-lg text-sm transition-all',
              color,
            )}
          >
            <Icon className="w-4 h-4" />
            {label}
          </Link>
        ))}
      </div>
    </div>
  )
}
