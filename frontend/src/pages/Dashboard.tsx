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
} from 'lucide-react'
import { useMixes, usePipelineStatus, useAIBudget } from '@/api/hooks'
import StatusBadge from '@/components/StatusBadge'
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

export default function Dashboard() {
  const { data: mixesData } = useMixes({ limit: 5 })
  const { data: pipeline } = usePipelineStatus()
  const { data: budget } = useAIBudget()

  const mixes = mixesData?.mixes ?? []
  const total = mixesData?.total ?? 0
  const uploaded = mixes.filter((m) => m.status === 'uploaded').length
  const processing = mixes.filter((m) => ['processing', 'generating'].includes(m.status)).length
  const failed = mixes.filter((m) => m.status === 'failed').length

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
        <StatCard icon={Upload} label="Uploaded" value={uploaded} accent="text-cyber-lime" />
        <StatCard
          icon={Clock}
          label="Processing"
          value={processing}
          accent="text-cyber-cyan"
          sub={pipeline?.active_step ? `Step: ${pipeline.active_step}` : undefined}
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
              ${budget.spent.toFixed(2)} / ${budget.budget_limit.toFixed(2)}
            </span>
          </div>
          <div className="h-3 bg-dark rounded-full overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full transition-all duration-700',
                budget.spent / budget.budget_limit > 0.9
                  ? 'bg-gradient-to-r from-cyber-red to-cyber-orange'
                  : budget.spent / budget.budget_limit > 0.7
                    ? 'bg-gradient-to-r from-gold to-cyber-orange'
                    : 'bg-gradient-to-r from-primary to-cyber-cyan',
              )}
              style={{ width: `${Math.min(100, (budget.spent / budget.budget_limit) * 100)}%` }}
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
                      <p className="text-[10px] text-gray-600 font-mono">{mix.filename}</p>
                    </div>
                  </div>
                  <StatusBadge status={mix.status} />
                </Link>
              ))
            )}
          </div>
        </div>

        {/* Pipeline Queue */}
        <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-primary/10">
            <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
              <Activity className="w-4 h-4 text-cyber-cyan" /> Pipeline
            </h2>
            <span className="text-[11px] font-mono text-gray-500">
              {pipeline?.queue_length ?? 0} queued
            </span>
          </div>
          <div className="p-5 space-y-4">
            {pipeline?.active_mix_id ? (
              <div className="flex items-center gap-3 p-3 bg-cyber-cyan/5 border border-cyber-cyan/20 rounded-lg">
                <div className="w-3 h-3 rounded-full bg-cyber-cyan animate-pulse" />
                <div>
                  <p className="text-sm text-cyber-cyan font-mono">Currently Processing</p>
                  <p className="text-[11px] text-gray-500">
                    Step: {pipeline.active_step ?? 'initializing'}
                  </p>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-3 p-3 bg-white/[0.02] rounded-lg">
                <CheckCircle2 className="w-4 h-4 text-primary/50" />
                <p className="text-sm text-gray-500">Pipeline idle</p>
              </div>
            )}

            {/* Recent completions */}
            {pipeline?.recent && pipeline.recent.length > 0 && (
              <div className="space-y-2">
                <p className="text-[10px] text-gray-600 font-mono uppercase tracking-wider">Recent</p>
                {pipeline.recent.slice(0, 4).map((item) => (
                  <div
                    key={item.mix_id}
                    className="flex items-center justify-between text-[11px] py-1"
                  >
                    <span className="text-gray-400 truncate max-w-[60%]">{item.title}</span>
                    <StatusBadge status={item.status} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

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
