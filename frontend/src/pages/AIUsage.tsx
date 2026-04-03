import { useState } from 'react'
import { Brain, DollarSign, TrendingUp, Zap, BarChart3 } from 'lucide-react'
import clsx from 'clsx'
import { format } from 'date-fns'
import { useAIUsage, useAIBudget, useAIUsageSummary } from '@/api/hooks'
import CostChart from '@/components/CostChart'

export default function AIUsage() {
  const { data: usage, isLoading: usageLoading } = useAIUsage()
  const { data: budget, isLoading: budgetLoading } = useAIBudget()
  const { data: summary } = useAIUsageSummary()
  const [page, setPage] = useState(1)

  const isLoading = usageLoading || budgetLoading

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="h-8 w-48 bg-surface-light rounded animate-pulse" />
        <div className="grid grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-28 bg-surface-light rounded-xl animate-pulse" />
          ))}
        </div>
        <div className="h-80 bg-surface-light rounded-xl animate-pulse" />
      </div>
    )
  }

  const spent = budget?.spent_this_month ?? 0
  const limit = budget?.monthly_budget ?? 10
  const remaining = budget?.remaining ?? limit - spent
  const pct = budget?.percentage_used ?? (spent / limit) * 100

  const records = usage?.items ?? []
  const totalRecords = usage?.total ?? 0

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
          <Brain className="w-6 h-6" /> AI Usage
        </h1>
        <p className="text-sm text-gray-500 mt-1">This month</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
          <div className="flex items-center gap-2 text-[11px] text-gray-500 font-mono uppercase tracking-wider mb-2">
            <DollarSign className="w-3.5 h-3.5" /> Total Spent
          </div>
          <p className="text-2xl font-bold text-gold">${spent.toFixed(2)}</p>
          <p className="text-[11px] text-gray-600 mt-1">of ${limit.toFixed(2)} budget</p>
        </div>

        <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
          <div className="flex items-center gap-2 text-[11px] text-gray-500 font-mono uppercase tracking-wider mb-2">
            <TrendingUp className="w-3.5 h-3.5" /> Total Calls
          </div>
          <p className="text-2xl font-bold text-accent">{totalRecords}</p>
          <p className="text-[11px] text-gray-600 mt-1">API calls this period</p>
        </div>

        <div className="bg-surface-light border border-primary/10 rounded-xl p-5">
          <div className="flex items-center gap-2 text-[11px] text-gray-500 font-mono uppercase tracking-wider mb-2">
            <Zap className="w-3.5 h-3.5" /> Remaining
          </div>
          <p
            className={clsx(
              'text-2xl font-bold',
              pct > 90 ? 'text-cyber-red' : pct > 70 ? 'text-cyber-orange' : 'text-primary',
            )}
          >
            ${remaining.toFixed(2)}
          </p>
          <p className="text-[11px] text-gray-600 mt-1">{Math.round(100 - pct)}% of budget left</p>
        </div>
      </div>

      {/* Budget Meter */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-mono text-gray-400">Budget Meter</h2>
          <span className="text-sm font-mono text-gray-500">{Math.round(pct)}%</span>
        </div>
        <div className="h-4 bg-dark rounded-full overflow-hidden relative">
          <div
            className={clsx(
              'h-full rounded-full transition-all duration-1000 relative',
              pct > 90
                ? 'bg-gradient-to-r from-cyber-orange to-cyber-red'
                : pct > 70
                  ? 'bg-gradient-to-r from-gold to-cyber-orange'
                  : 'bg-gradient-to-r from-primary to-cyber-cyan',
            )}
            style={{ width: `${Math.min(100, pct)}%` }}
          >
            <div className="absolute inset-0 bg-white/10 animate-pulse-slow rounded-full" />
          </div>
        </div>
      </div>

      {/* Cost Summary by Operation */}
      {summary?.items && summary.items.length > 0 && (
        <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-4">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <BarChart3 className="w-4 h-4" /> Cost Breakdown by Operation
          </h2>
          <CostChart data={summary.items} />
        </div>
      )}

      {/* Usage Records Table */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        <div className="px-6 py-4 border-b border-primary/10">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <Brain className="w-4 h-4" /> Usage Records
          </h2>
        </div>
        <div className="divide-y divide-white/5">
          {records.length === 0 ? (
            <div className="px-6 py-8 text-center text-gray-600 text-sm">No usage records yet.</div>
          ) : (
            records.map((item) => (
              <div key={item.id} className="flex items-center justify-between px-6 py-3">
                <div>
                  <p className="text-sm text-gray-300">
                    <span className="text-primary">{item.operation}</span>
                    {item.mix_id && <span className="text-gray-600 ml-2 text-xs">mix:{item.mix_id.slice(0, 8)}</span>}
                  </p>
                  <p className="text-[10px] text-gray-600 font-mono">
                    {item.provider}/{item.model} &middot; {item.input_tokens + item.output_tokens} tokens &middot;{' '}
                    {format(new Date(item.created_at), 'MMM d, HH:mm')}
                  </p>
                </div>
                <span className="text-sm font-mono text-gold">${item.cost_usd.toFixed(4)}</span>
              </div>
            ))
          )}
        </div>
        {/* Pagination hint */}
        {totalRecords > records.length && (
          <div className="px-6 py-3 border-t border-white/5 text-center">
            <button
              onClick={() => setPage(page + 1)}
              className="text-xs text-primary hover:text-primary/80 transition-colors font-mono"
            >
              Load more ({totalRecords - records.length} remaining)
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
