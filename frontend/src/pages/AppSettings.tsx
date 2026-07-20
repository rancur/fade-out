import { useMemo, useState } from 'react'
import {
  Settings,
  Save,
  Brain,
  FolderOpen,
  Workflow,
  Youtube,
  Cloud,
  History,
  Wrench,
  Shield,
  Lock,
  RotateCcw,
  Loader2,
  Undo2,
  type LucideIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import {
  useSettingsSchema,
  useUpdateSettingValues,
  type SettingItem,
  type SettingValue,
} from '@/api/hooks'
import SetupWizard from '@/components/OAuthConnect'

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  Paths: FolderOpen,
  Pipeline: Workflow,
  AI: Brain,
  YouTube: Youtube,
  SoundCloud: Cloud,
  Activity: History,
  Advanced: Wrench,
}

const CATEGORY_BLURBS: Record<string, string> = {
  Paths: 'Effective folders the pipeline reads and writes. These are container mounts configured at deploy time.',
  Pipeline: 'How drops become published mixes: review gates, premiere scheduling, and detection tuning.',
  AI: 'Providers, models, budget, and API keys for descriptions and artwork.',
  YouTube: 'Quota budget, playlist naming, and upload credentials.',
  SoundCloud: 'Account credentials and OAuth tokens.',
  Activity: 'Retention for the activity event log.',
  Advanced: 'Upgrade, cross-linking, and deploy-bound safety values.',
}

// ---------------------------------------------------------------------------
// Field helpers
// ---------------------------------------------------------------------------

type Staged = Record<string, SettingValue>

function validationError(item: SettingItem, value: SettingValue): string | null {
  if (value === null) return null
  if (item.type === 'int' || item.type === 'float') {
    const n = typeof value === 'number' ? value : NaN
    if (Number.isNaN(n)) return 'Must be a number'
    if (item.type === 'int' && !Number.isInteger(n)) return 'Must be a whole number'
    if (item.min !== null && n < item.min) return `Must be at least ${item.min}`
    if (item.max !== null && n > item.max) return `Must be at most ${item.max}`
  }
  return null
}

function SourceBadge({ source }: { source: SettingItem['source'] }) {
  return (
    <span
      className={clsx(
        'px-1.5 py-0.5 rounded text-[10px] font-mono uppercase tracking-wider',
        source === 'db' && 'bg-primary/10 text-primary',
        source === 'env' && 'bg-gold/10 text-gold',
        source === 'default' && 'bg-gray-800 text-gray-500',
      )}
    >
      {source === 'db' ? 'app' : source}
    </span>
  )
}

function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <button
      onClick={() => !disabled && onChange(!checked)}
      disabled={disabled}
      aria-pressed={checked}
      className={clsx(
        'relative w-11 h-6 rounded-full transition-colors shrink-0',
        checked ? 'bg-primary' : 'bg-gray-700',
        disabled && 'opacity-40 cursor-not-allowed',
      )}
    >
      <span
        className={clsx(
          'absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform shadow',
          checked ? 'translate-x-[22px]' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}

const inputClass =
  'w-full px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono disabled:opacity-50'

function SettingField({
  item,
  staged,
  onStage,
  onUnstage,
}: {
  item: SettingItem
  staged: SettingValue | undefined
  onStage: (value: SettingValue) => void
  onUnstage: () => void
}) {
  const isDirty = staged !== undefined
  const effective = isDirty ? staged : item.value
  const error = isDirty ? validationError(item, staged) : null
  // A staged null means "reset to env/default".
  const stagedReset = isDirty && staged === null

  const control = (() => {
    if (!item.editable) {
      return (
        <div className="flex items-center gap-2 min-w-0">
          <code className="text-sm text-gray-400 font-mono truncate px-3 py-2 bg-dark border border-primary/5 rounded-lg flex-1">
            {String(item.value ?? '') || '(not set)'}
          </code>
        </div>
      )
    }

    switch (item.type) {
      case 'bool':
        return (
          <Toggle checked={Boolean(effective)} onChange={(v) => onStage(v)} />
        )
      case 'enum':
        return (
          <select
            value={String(effective ?? '')}
            onChange={(e) => onStage(e.target.value)}
            className={clsx(inputClass, 'appearance-none cursor-pointer')}
          >
            {(item.choices ?? []).map((c) => (
              <option key={c} value={c}>
                {c.charAt(0).toUpperCase() + c.slice(1)}
              </option>
            ))}
          </select>
        )
      case 'int':
      case 'float':
        return (
          <input
            type="number"
            value={stagedReset ? '' : String(effective ?? '')}
            min={item.min ?? undefined}
            max={item.max ?? undefined}
            step={item.type === 'float' ? 'any' : 1}
            placeholder={stagedReset ? String(item.default ?? '') : undefined}
            onChange={(e) => {
              const raw = e.target.value
              onStage(raw === '' ? NaN : Number(raw))
            }}
            className={clsx(inputClass, error && 'border-red-500/60')}
          />
        )
      case 'secret':
        return (
          <div className="flex items-center gap-2">
            <input
              type="password"
              autoComplete="new-password"
              value={isDirty && typeof staged === 'string' ? staged : ''}
              placeholder={
                stagedReset
                  ? 'will be cleared on save'
                  : item.has_value
                    ? '•••• saved (write-only)'
                    : 'not set'
              }
              onChange={(e) => {
                if (e.target.value === '') onUnstage()
                else onStage(e.target.value)
              }}
              className={clsx(inputClass, 'flex-1')}
            />
            {item.has_value && !isDirty && (
              <button
                onClick={() => onStage(null)}
                title="Clear the saved value (falls back to env)"
                className="p-2 text-gray-500 hover:text-red-400 transition-colors"
              >
                <RotateCcw className="w-4 h-4" />
              </button>
            )}
          </div>
        )
      default:
        return (
          <input
            type="text"
            value={stagedReset ? '' : String(effective ?? '')}
            placeholder={stagedReset ? String(item.default ?? '') : undefined}
            onChange={(e) => onStage(e.target.value)}
            className={inputClass}
          />
        )
    }
  })()

  return (
    <div
      className={clsx(
        'grid grid-cols-1 sm:grid-cols-[1fr_260px] gap-2 sm:gap-6 items-start py-4',
        isDirty && 'bg-primary/[0.03] -mx-3 px-3 rounded-lg',
      )}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <p className="text-sm text-gray-300">{item.label}</p>
          {item.type === 'secret' && (
            <Lock className="w-3 h-3 text-gray-600" aria-label="write-only secret" />
          )}
          <SourceBadge source={item.source} />
          {!item.editable && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase tracking-wider bg-gray-800 text-gray-500">
              configured at deploy
            </span>
          )}
          {isDirty && (
            <button
              onClick={onUnstage}
              className="flex items-center gap-1 text-[10px] font-mono text-primary hover:text-primary/70"
            >
              <Undo2 className="w-3 h-3" /> undo
            </button>
          )}
        </div>
        <p className="text-[11px] text-gray-600 mt-1 leading-relaxed">{item.help}</p>
        {item.editable && item.source === 'db' && item.type !== 'secret' && !isDirty && (
          <button
            onClick={() => onStage(null)}
            className="mt-1 text-[10px] font-mono text-gray-500 hover:text-gold transition-colors"
          >
            reset to default{item.default !== null && item.default !== '' ? ` (${String(item.default)})` : ''}
          </button>
        )}
        {error && <p className="text-[11px] text-red-400 mt-1">{error}</p>}
      </div>
      <div>{control}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function AppSettings() {
  const { data: schema, isLoading } = useSettingsSchema()
  const update = useUpdateSettingValues()

  const [tab, setTab] = useState<string | null>(null)
  const [staged, setStaged] = useState<Staged>({})

  const categories = schema?.categories ?? []
  const activeTab = tab ?? categories[0]

  const byCategory = useMemo(() => {
    const map: Record<string, SettingItem[]> = {}
    for (const s of schema?.settings ?? []) {
      ;(map[s.category] ??= []).push(s)
    }
    return map
  }, [schema])

  const itemsByKey = useMemo(() => {
    const map: Record<string, SettingItem> = {}
    for (const s of schema?.settings ?? []) map[s.key] = s
    return map
  }, [schema])

  const dirtyCount = Object.keys(staged).length
  const hasErrors = Object.entries(staged).some(([key, value]) => {
    const item = itemsByKey[key]
    return item ? validationError(item, value) !== null : false
  })

  const dirtyInCategory = (cat: string) =>
    Object.keys(staged).filter((k) => itemsByKey[k]?.category === cat).length

  const handleSave = () => {
    update.mutate(staged, {
      onSuccess: () => {
        setStaged({})
        toast.success(`Saved ${dirtyCount} setting${dirtyCount === 1 ? '' : 's'}`)
      },
      onError: (err) => {
        const detail =
          (err as { response?: { data?: { detail?: string } } })?.response?.data
            ?.detail ?? 'Failed to save settings'
        toast.error(detail)
      },
    })
  }

  if (isLoading || !schema) {
    return <div className="h-96 bg-surface-light rounded-xl animate-pulse" />
  }

  return (
    <div className="space-y-8 max-w-4xl pb-24">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
          <Settings className="w-6 h-6" /> Settings
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          Everything the app needs to run — values saved here override environment defaults
        </p>
      </div>

      {/* Platform Connections (existing OAuth flows, untouched) */}
      <SetupWizard />

      {/* Category tabs */}
      <div className="border-b border-primary/10">
        <div className="flex gap-1 -mb-px overflow-x-auto">
          {categories.map((cat) => {
            const Icon = CATEGORY_ICONS[cat] ?? Settings
            const dirty = dirtyInCategory(cat)
            return (
              <button
                key={cat}
                onClick={() => setTab(cat)}
                className={clsx(
                  'flex items-center gap-2 px-4 py-3 text-sm font-mono border-b-2 transition-all whitespace-nowrap',
                  activeTab === cat
                    ? 'text-primary border-primary'
                    : 'text-gray-500 border-transparent hover:text-gray-300 hover:border-primary/20',
                )}
              >
                <Icon className="w-4 h-4" />
                {cat}
                {dirty > 0 && (
                  <span className="px-1.5 rounded-full bg-primary/20 text-primary text-[10px]">
                    {dirty}
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      {/* Active category */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6">
        <p className="text-[11px] text-gray-600 mb-2">{CATEGORY_BLURBS[activeTab] ?? ''}</p>
        {(activeTab === 'SoundCloud' || activeTab === 'YouTube') && (
          <p className="text-[11px] text-gold/80 mb-2">
            Connection status and OAuth reconnect are handled in Platform Connections at the
            top of this page — credentials below are the raw values behind that flow.
          </p>
        )}
        <div className="divide-y divide-primary/5">
          {(byCategory[activeTab] ?? []).map((item) => (
            <SettingField
              key={item.key}
              item={item}
              staged={staged[item.key]}
              onStage={(value) => setStaged((s) => ({ ...s, [item.key]: value }))}
              onUnstage={() =>
                setStaged((s) => {
                  const next = { ...s }
                  delete next[item.key]
                  return next
                })
              }
            />
          ))}
        </div>
      </div>

      {/* Security note */}
      <div className="flex items-start gap-3 p-4 bg-gold/5 border border-gold/20 rounded-xl">
        <Shield className="w-5 h-5 text-gold shrink-0 mt-0.5" />
        <div>
          <p className="text-sm text-gold">Secrets are write-only</p>
          <p className="text-[11px] text-gray-500 mt-1">
            API keys, tokens, and passwords are stored server-side and never sent back to the
            browser — the UI only ever knows whether a value is saved. Values saved here take
            priority over environment variables.
          </p>
        </div>
      </div>

      {/* Save bar */}
      {dirtyCount > 0 && (
        <div className="fixed bottom-0 left-0 right-0 z-40 bg-surface-light/95 backdrop-blur border-t border-primary/20">
          <div className="max-w-4xl mx-auto px-6 py-3 flex items-center justify-between gap-4">
            <p className="text-sm text-gray-400 font-mono">
              {dirtyCount} unsaved change{dirtyCount === 1 ? '' : 's'}
              {hasErrors && <span className="text-red-400 ml-2">fix invalid values to save</span>}
            </p>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setStaged({})}
                className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors"
              >
                Discard
              </button>
              <button
                onClick={handleSave}
                disabled={update.isPending || hasErrors}
                className="flex items-center gap-2 px-5 py-2 bg-primary text-dark font-semibold rounded-lg text-sm hover:bg-primary/90 disabled:opacity-50 transition-all"
              >
                {update.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Save className="w-4 h-4" />
                )}
                {update.isPending ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
