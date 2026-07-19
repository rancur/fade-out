import clsx from 'clsx'
import { ArrowRight } from 'lucide-react'
import type { ProposalField } from '@/api/hooks'

// ---------- Lightweight line diff (LCS, no deps) ----------

export interface DiffLine {
  type: 'same' | 'add' | 'del'
  text: string
}

/** Simple LCS-based line diff. Falls back to full replace on huge inputs. */
export function diffLines(current: string, proposed: string): DiffLine[] {
  const a = current.split('\n')
  const b = proposed.split('\n')

  // Guard against pathological O(n*m) blowups — descriptions are small anyway.
  if (a.length * b.length > 250_000) {
    return [
      ...a.map((text) => ({ type: 'del' as const, text })),
      ...b.map((text) => ({ type: 'add' as const, text })),
    ]
  }

  // LCS table
  const n = a.length
  const m = b.length
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] })
      i++
    } else {
      out.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < n) out.push({ type: 'del', text: a[i++] })
  while (j < m) out.push({ type: 'add', text: b[j++] })
  return out
}

/** Parse a tags proposal value (JSON array string) into chips, or null. */
function parseTags(value: string | null): string[] | null {
  if (!value) return null
  try {
    const parsed = JSON.parse(value)
    if (Array.isArray(parsed)) return parsed.map(String)
  } catch {
    // not JSON — fall through
  }
  return null
}

function TagChips({ tags, tone }: { tags: string[]; tone: 'old' | 'new' }) {
  if (tags.length === 0) {
    return <span className="text-[11px] text-gray-600 font-mono italic">(none)</span>
  }
  return (
    <span className="flex flex-wrap gap-1">
      {tags.map((t, idx) => (
        <span
          key={`${t}-${idx}`}
          className={clsx(
            'text-[10px] px-1.5 py-0.5 rounded font-mono',
            tone === 'new' ? 'bg-cyber-lime/10 text-cyber-lime' : 'bg-white/5 text-gray-500',
          )}
        >
          {t}
        </span>
      ))}
    </span>
  )
}

// ---------- Diff renderer ----------

interface DiffViewProps {
  field: ProposalField
  current: string | null
  proposed: string | null
}

/**
 * Field-aware diff: multiline line-diff for descriptions, chip diff for tags,
 * before → after for everything else.
 */
export default function DiffView({ field, current, proposed }: DiffViewProps) {
  if (field === 'description') {
    const lines = diffLines(current ?? '', proposed ?? '')
    return (
      <pre className="text-[11px] font-mono bg-dark rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-words max-h-72 overflow-y-auto">
        {lines.map((l, idx) => (
          <div
            key={idx}
            className={clsx(
              'px-1 -mx-1 rounded-sm',
              l.type === 'add' && 'bg-cyber-lime/10 text-cyber-lime',
              l.type === 'del' && 'bg-cyber-red/10 text-cyber-red/80 line-through decoration-cyber-red/40',
              l.type === 'same' && 'text-gray-400',
            )}
          >
            <span className="select-none mr-1.5 text-gray-600">
              {l.type === 'add' ? '+' : l.type === 'del' ? '-' : ' '}
            </span>
            {l.text || ' '}
          </div>
        ))}
      </pre>
    )
  }

  if (field === 'tags') {
    const oldTags = parseTags(current) ?? (current ? [current] : [])
    const newTags = parseTags(proposed) ?? (proposed ? [proposed] : [])
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <TagChips tags={oldTags} tone="old" />
        <ArrowRight className="w-3.5 h-3.5 text-gray-600 shrink-0" />
        <TagChips tags={newTags} tone="new" />
      </div>
    )
  }

  // title / thumbnail / playlist: before → after
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
      <span className="text-gray-500 line-through decoration-gray-600 break-all">
        {current || <span className="italic no-underline">(empty)</span>}
      </span>
      <ArrowRight className="w-3.5 h-3.5 text-gray-600 shrink-0" />
      <span className="text-cyber-lime break-all">{proposed || '(empty)'}</span>
    </div>
  )
}
