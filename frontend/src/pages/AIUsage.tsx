import { useState } from 'react'
import { Brain, DollarSign, TrendingUp, Zap, Music2 } from 'lucide-react'
import clsx from 'clsx'
import { format } from 'date-fns'
import { useAIUsage, useAIBudget, useUpdateAIBudget } from '@/api/hooks'
import CostChart from '@/components/CostChart'
import { toast } from 'sonner'

export default function AIUsage() {
  const { data: usage, isLoading: usageLoading } = useAIUsage()
  const { data: budget, isLoading: budgetLoading } = useAIBudget()
  const updateBudget = useUpdateAIBudget()
  const [newBudget, setNewBudget] = useState<string>('')

  const isLoading = usageLoading || budgetLoading

  const handleUpdateBudget = () => {
    const val = parseFloat(newBudget)
    if (isNaN(val) || val <= 0) {
      toast.error('Enter a valid budget amount')
      return
    }
    updateBudget.mutate(val, {
      onSuccess: () => {
        toast.success('Budget updated')
        setNewBudget('')
      },
      onError: () => toast.error('Failed to update budget'),
    })
  }

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

  const spent = budget?.spent ?? 0
  const limit = budget?.budget_limit ?? 10
  const pct = (spent / limit) * 100

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
          <Brain className="w-6 h-6" /> AI Usage
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          {usage?.month ? format(new Date(usage.month + '-01'), 'MMMM yyyy') : 'This month'}
        </p>
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
            <TrendingUp className="w-3.5 h-3.5" /> Avg per Mix
          </div>
          <p className="text-2xl font-bold text-accent">
            ${usage?.per_mix.length ? (spent / usage.per_mix.length).toFixed(3) : '0.000'}
          </p>
          <p className="text-[11px] text-gray-600 mt-1">{usage?.per_mix.length ?? 0} mixes processed</p>
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
            ${Math.max(0, limit - spent).toFixed(2)}
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

        {/* Update budget */}
        <div className="flex items-center gap-3 pt-2">
          <label className="text-[11px] text-gray-500 font-mono shrink-0">Set Budget:</label>
          <div className="flex items-center gap-2">
            <span className="text-gray-500">$</span>
            <input
              type="number"
              step="0.50"
              min="1"
              value={newBudget}
              onChange={(e) => setNewBudget(e.target.value)}
              placeholder={limit.toFixed(2)}
              className="w-28 px-3 py-1.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
            />
            <button
              onClick={handleUpdateBudget}
              disabled={updateBudget.isPending}
              className="px-4 py-1.5 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 disabled:opacity-50 transition-all"
            >
              Update
            </button>
          </div>
        </div>
      </div>

      {/* Cost Chart */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-4">
        <h2 className="text-sm font-mono text-gray-400">Daily Cost Breakdown</h2>
        <CostChart data={usage?.daily_costs ?? []} />
      </div>

      {/* Per-Mix Costs */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        <div className="px-6 py-4 border-b border-primary/10">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <Music2 className="w-4 h-4" /> Per-Mix Costs
          </h2>
        </div>
        <div className="divide-y divide-white/5">
          {usage?.per_mix.length === 0 ? (
            <div className="px-6 py-8 text-center text-gray-600 text-sm">No mix costs recorded yet.</div>
          ) : (
            usage?.per_mix.map((item) => (
              <div key={item.mix_id} className="flex items-center justify-between px-6 py-3">
                <div>
                  <p className="text-sm text-gray-300">{item.title}</p>
                  <p className="text-[10px] text-gray-600 font-mono">{format(new Date(item.date), 'MMM d, yyyy')}</p>
                </div>
                <span className="text-sm font-mono text-gold">${item.cost.toFixed(3)}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
