import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import { formatDistanceToNow } from 'date-fns'
import { toast } from 'sonner'
import {
  AlertCircle,
  ArrowLeft,
  Check,
  CheckCheck,
  ListChecks,
  Loader2,
  Music2,
  Pencil,
  RotateCcw,
  X,
} from 'lucide-react'
import {
  useApproveBulk,
  useCreateProposal,
  useProposalAction,
  useProposals,
  type Proposal,
} from '@/api/hooks'
import DiffView from '@/components/DiffView'
import { ProposalStatusChip } from './CatalogMixEditor'

const STATUS_FILTERS = ['draft', 'approved', 'applying', 'applied', 'rejected', 'failed', 'all'] as const

function CreatedByChip({ createdBy }: { createdBy: 'ai' | 'user' }) {
  return (
    <span
      className={clsx(
        'text-[10px] font-mono px-1.5 py-0.5 rounded uppercase tracking-wider',
        createdBy === 'ai' ? 'bg-cyber-magenta/10 text-cyber-magenta' : 'bg-primary/10 text-primary',
      )}
    >
      {createdBy}
    </span>
  )
}

function ProposalRow({
  proposal,
  selected,
  onToggleSelect,
}: {
  proposal: Proposal
  selected: boolean
  onToggleSelect: (id: string) => void
}) {
  const action = useProposalAction()
  const createProposal = useCreateProposal()
  const [editing, setEditing] = useState(false)
  const [editValue, setEditValue] = useState(proposal.proposed_value ?? '')

  const selectable = proposal.status === 'draft' || proposal.status === 'failed'
  const canApprove = proposal.status === 'draft' || proposal.status === 'failed'
  const canReject = ['draft', 'approved', 'failed'].includes(proposal.status)

  const approve = () =>
    action.mutate(
      { id: proposal.id, action: 'approve' },
      {
        onSuccess: () =>
          toast.success(proposal.status === 'failed' ? 'Proposal re-approved' : 'Proposal approved'),
        onError: () => toast.error('Failed to approve proposal'),
      },
    )

  const reject = () =>
    action.mutate(
      { id: proposal.id, action: 'reject' },
      {
        onSuccess: () => toast.success('Proposal rejected'),
        onError: () => toast.error('Failed to reject proposal'),
      },
    )

  const confirmEdit = () => {
    createProposal.mutate(
      {
        mixId: proposal.mix_id,
        platform: proposal.platform,
        field: proposal.field,
        proposed_value: editValue,
        current_value: proposal.current_value,
        status: 'approved',
      },
      {
        onSuccess: () => {
          // Supersede the original with the edited, approved user proposal
          action.mutate(
            { id: proposal.id, action: 'reject' },
            {
              onSuccess: () => toast.success('Edited proposal approved'),
              onError: () => toast.error('Edited proposal saved, but rejecting the original failed'),
            },
          )
          setEditing(false)
        },
        onError: () => toast.error('Failed to save edited proposal'),
      },
    )
  }

  const busy = action.isPending || createProposal.isPending

  return (
    <div className="border-b border-white/5 last:border-0 px-4 py-3 space-y-2 hover:bg-white/[0.02]">
      <div className="flex flex-wrap items-center gap-2">
        {selectable && (
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelect(proposal.id)}
            aria-label={`Select ${proposal.field} proposal`}
            className="accent-[#7CB342] cursor-pointer"
          />
        )}
        <ProposalStatusChip status={proposal.status} />
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-dark text-gray-500">
          {proposal.platform}
        </span>
        <span className="text-xs text-gray-300 font-mono">{proposal.field}</span>
        <CreatedByChip createdBy={proposal.created_by} />
        {proposal.created_at && (
          <span className="text-[10px] text-gray-600 font-mono">
            {formatDistanceToNow(new Date(proposal.created_at), { addSuffix: true })}
          </span>
        )}

        {/* Actions */}
        <div className="ml-auto flex items-center gap-1.5">
          {busy && <Loader2 className="w-3.5 h-3.5 text-gray-500 animate-spin" />}
          {proposal.status === 'draft' && !editing && (
            <button
              onClick={() => {
                setEditValue(proposal.proposed_value ?? '')
                setEditing(true)
              }}
              title="Edit then approve"
              className="p-1.5 rounded-lg text-gray-500 hover:text-accent border border-white/10 hover:border-accent/40 transition-colors"
            >
              <Pencil className="w-3.5 h-3.5" />
            </button>
          )}
          {canApprove && !editing && (
            <button
              onClick={approve}
              disabled={busy}
              title={proposal.status === 'failed' ? 'Re-approve' : 'Approve'}
              className="p-1.5 rounded-lg text-cyber-lime border border-cyber-lime/30 hover:bg-cyber-lime/10 disabled:opacity-40 transition-colors"
            >
              {proposal.status === 'failed' ? (
                <RotateCcw className="w-3.5 h-3.5" />
              ) : (
                <Check className="w-3.5 h-3.5" />
              )}
            </button>
          )}
          {canReject && !editing && (
            <button
              onClick={reject}
              disabled={busy}
              title="Reject"
              className="p-1.5 rounded-lg text-cyber-red border border-cyber-red/30 hover:bg-cyber-red/10 disabled:opacity-40 transition-colors"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Diff or edit form */}
      {editing ? (
        <div className="space-y-2">
          <textarea
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            rows={proposal.field === 'description' ? 10 : 2}
            aria-label="Edit proposed value"
            className="w-full px-3 py-2 bg-surface border border-accent/30 rounded-lg text-xs text-gray-200 font-mono leading-relaxed focus:border-accent/60 focus:outline-none resize-y"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={confirmEdit}
              disabled={busy || !editValue.trim()}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyber-lime/10 text-cyber-lime border border-cyber-lime/30 text-[11px] font-mono uppercase tracking-wider hover:bg-cyber-lime/20 disabled:opacity-40 transition-all"
            >
              <Check className="w-3.5 h-3.5" /> Approve edited
            </button>
            <button
              onClick={() => setEditing(false)}
              className="px-3 py-1.5 rounded-lg text-gray-500 border border-white/10 text-[11px] font-mono uppercase tracking-wider hover:text-gray-300 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <DiffView
          field={proposal.field}
          current={proposal.current_value}
          proposed={proposal.proposed_value}
        />
      )}

      {/* Error (failed proposals) */}
      {proposal.status === 'failed' && proposal.error && (
        <p className="flex items-start gap-1.5 text-[11px] text-cyber-red font-mono bg-cyber-red/5 border border-cyber-red/20 rounded-lg px-3 py-2">
          <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> {proposal.error}
        </p>
      )}
    </div>
  )
}

function ProposalGroup({
  mixId,
  title,
  proposals,
  selectedIds,
  onToggleSelect,
}: {
  mixId: string
  title: string
  proposals: Proposal[]
  selectedIds: Set<string>
  onToggleSelect: (id: string) => void
}) {
  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 bg-white/[0.02] border-b border-white/5">
        <Music2 className="w-3.5 h-3.5 text-primary shrink-0" />
        <Link
          to={`/catalog/${mixId}`}
          className="text-xs font-semibold text-gray-200 hover:text-primary truncate transition-colors"
        >
          {title}
        </Link>
        <span className="text-[10px] text-gray-600 font-mono ml-auto shrink-0">
          {proposals.length} proposal{proposals.length === 1 ? '' : 's'}
        </span>
      </div>
      {proposals.map((p) => (
        <ProposalRow
          key={p.id}
          proposal={p}
          selected={selectedIds.has(p.id)}
          onToggleSelect={onToggleSelect}
        />
      ))}
    </div>
  )
}

export default function ReviewQueue() {
  const [searchParams, setSearchParams] = useSearchParams()
  const mixId = searchParams.get('mix_id') ?? ''
  const status = searchParams.get('status') ?? 'draft'

  const { data, isLoading, isError } = useProposals({
    status: status === 'all' ? undefined : status,
    mix_id: mixId || undefined,
    limit: 200,
  })

  const approveBulk = useApproveBulk()
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  const items = useMemo(() => data?.items ?? [], [data])

  const groups = useMemo(() => {
    const map = new Map<string, { title: string; proposals: Proposal[] }>()
    for (const p of items) {
      const entry = map.get(p.mix_id) ?? { title: p.mix_title ?? p.mix_id, proposals: [] }
      entry.proposals.push(p)
      map.set(p.mix_id, entry)
    }
    return Array.from(map.entries())
  }, [items])

  const selectableIds = useMemo(
    () => items.filter((p) => p.status === 'draft' || p.status === 'failed').map((p) => p.id),
    [items],
  )
  const selectedVisible = selectableIds.filter((id) => selectedIds.has(id))

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    setSelectedIds((prev) =>
      selectedVisible.length === selectableIds.length && selectableIds.length > 0
        ? new Set([...prev].filter((id) => !selectableIds.includes(id)))
        : new Set([...prev, ...selectableIds]),
    )
  }

  const bulkApprove = () => {
    approveBulk.mutate(selectedVisible, {
      onSuccess: (res) => {
        toast.success(`${res.approved} approved${res.applying ? ', applying' : ''}`)
        setSelectedIds(new Set())
      },
      onError: () => toast.error('Bulk approve failed'),
    })
  }

  const setParam = (key: string, value: string | null) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value) next.set(key, value)
        else next.delete(key)
        return next
      },
      { replace: true },
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link
            to="/catalog"
            className="flex items-center gap-1.5 text-[11px] text-gray-500 hover:text-primary font-mono transition-colors mb-2"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> Catalog
          </Link>
          <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
            <ListChecks className="w-6 h-6" /> Review Queue
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {data?.total ?? 0} proposal{(data?.total ?? 0) === 1 ? '' : 's'}
            {status !== 'all' ? ` (${status})` : ''}
          </p>
        </div>

        {selectedVisible.length > 0 && (
          <button
            onClick={bulkApprove}
            disabled={approveBulk.isPending}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyber-lime/10 text-cyber-lime border border-cyber-lime/30 text-xs font-mono uppercase tracking-wider hover:bg-cyber-lime/20 disabled:opacity-50 transition-all"
          >
            {approveBulk.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <CheckCheck className="w-4 h-4" />
            )}
            Approve selected ({selectedVisible.length})
          </button>
        )}
      </div>

      {/* Filters */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {STATUS_FILTERS.map((s) => (
            <button
              key={s}
              onClick={() => setParam('status', s === 'draft' ? null : s)}
              className={clsx(
                'px-3 py-1.5 rounded-lg text-[11px] font-mono uppercase tracking-wider border transition-all',
                status === s
                  ? 'bg-primary/10 text-primary border-primary/30'
                  : 'bg-surface text-gray-500 border-white/5 hover:border-primary/20 hover:text-gray-300',
              )}
            >
              {s}
            </button>
          ))}

          {selectableIds.length > 0 && (
            <button
              onClick={toggleSelectAll}
              className="ml-auto text-[11px] font-mono text-gray-500 hover:text-primary transition-colors"
            >
              {selectedVisible.length === selectableIds.length ? 'clear selection' : 'select all'}
            </button>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[200px] flex-1 max-w-sm">
            <Music2 className="w-3.5 h-3.5 text-gray-600 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={mixId}
              onChange={(e) => setParam('mix_id', e.target.value.trim() || null)}
              placeholder="filter by mix id…"
              className="w-full bg-surface border border-white/10 rounded-lg pl-8 pr-7 py-1.5 text-xs font-mono text-gray-300 placeholder:text-gray-600 focus:outline-none focus:border-primary/40"
            />
            {mixId && (
              <button
                onClick={() => setParam('mix_id', null)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                title="Clear mix filter"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Groups */}
      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-gray-500 font-mono py-10 justify-center">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading proposals…
        </div>
      ) : isError ? (
        <p className="text-sm text-cyber-red font-mono py-10 text-center">Failed to load proposals.</p>
      ) : groups.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <ListChecks className="w-10 h-10 text-gray-700 mb-3" />
          <p className="font-pixel text-xs">Queue clear</p>
          <p className="text-sm mt-2">
            No {status === 'all' ? '' : `${status} `}proposals
            {mixId ? ' for this mix' : ''}. Run AI Improve or edit a mix to create some.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {groups.map(([groupMixId, group]) => (
            <ProposalGroup
              key={groupMixId}
              mixId={groupMixId}
              title={group.title}
              proposals={group.proposals}
              selectedIds={selectedIds}
              onToggleSelect={toggleSelect}
            />
          ))}
        </div>
      )}
    </div>
  )
}
