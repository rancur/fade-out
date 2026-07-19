import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import clsx from 'clsx'
import { formatDistanceToNow } from 'date-fns'
import { toast } from 'sonner'
import {
  ArrowLeft,
  ExternalLink,
  Image as ImageIcon,
  Info,
  ListChecks,
  Loader2,
  Lock,
  Music,
  Save,
  Send,
  Unlock,
  X,
} from 'lucide-react'
import {
  useEditCatalogMix,
  useLockTitle,
  useMix,
  useProposals,
  type CatalogMixEditBody,
  type PlatformEdit,
  type Proposal,
} from '@/api/hooks'

interface PlatformFields {
  title: string
  description: string
  tags: string[]
}

const emptyFields: PlatformFields = { title: '', description: '', tags: [] }

function sameTags(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((t, i) => t === b[i])
}

function platformDiff(current: PlatformFields, base: PlatformFields): PlatformEdit | null {
  const edit: PlatformEdit = {}
  if (current.title !== base.title) edit.title = current.title
  if (current.description !== base.description) edit.description = current.description
  if (!sameTags(current.tags, base.tags)) edit.tags = current.tags
  return Object.keys(edit).length > 0 ? edit : null
}

function TagsInput({
  tags,
  onChange,
  idPrefix,
}: {
  tags: string[]
  onChange: (tags: string[]) => void
  idPrefix: string
}) {
  const [draft, setDraft] = useState('')

  const commit = () => {
    const value = draft.trim().replace(/,+$/, '')
    if (value && !tags.includes(value)) onChange([...tags, value])
    setDraft('')
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 bg-surface border border-white/10 rounded-lg px-2 py-1.5 focus-within:border-primary/40 transition-colors">
      {tags.map((t) => (
        <span
          key={t}
          className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded bg-primary/10 text-primary/90 font-mono"
        >
          {t}
          <button
            onClick={() => onChange(tags.filter((x) => x !== t))}
            className="text-primary/50 hover:text-cyber-red transition-colors"
            aria-label={`Remove tag ${t}`}
          >
            <X className="w-3 h-3" />
          </button>
        </span>
      ))}
      <input
        id={`${idPrefix}-tags`}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault()
            commit()
          } else if (e.key === 'Backspace' && !draft && tags.length) {
            onChange(tags.slice(0, -1))
          }
        }}
        onBlur={commit}
        placeholder={tags.length === 0 ? 'add tags…' : ''}
        className="flex-1 min-w-[80px] bg-transparent text-xs font-mono text-gray-300 placeholder:text-gray-600 focus:outline-none py-0.5"
      />
    </div>
  )
}

const proposalStatusStyle: Record<string, string> = {
  draft: 'text-gold border-gold/40 bg-gold/10',
  approved: 'text-cyber-cyan border-cyber-cyan/40 bg-cyber-cyan/10',
  applying: 'text-cyber-cyan border-cyber-cyan/40 bg-cyber-cyan/10 animate-pulse',
  applied: 'text-cyber-lime border-cyber-lime/40 bg-cyber-lime/10',
  rejected: 'text-gray-500 border-gray-600 bg-white/5',
  failed: 'text-cyber-red border-cyber-red/40 bg-cyber-red/10',
}

export function ProposalStatusChip({ status }: { status: string }) {
  return (
    <span
      className={clsx(
        'text-[10px] px-2 py-0.5 rounded-full font-mono uppercase tracking-wider border',
        proposalStatusStyle[status] ?? proposalStatusStyle.draft,
      )}
    >
      {status}
    </span>
  )
}

function PlatformPanel({
  platform,
  fields,
  onChange,
  titleLocked,
  url,
}: {
  platform: 'youtube' | 'soundcloud'
  fields: PlatformFields
  onChange: (f: PlatformFields) => void
  titleLocked: boolean
  url: string | null
}) {
  const label = platform === 'youtube' ? 'YouTube' : 'SoundCloud'
  const accent = platform === 'youtube' ? 'text-cyber-red' : 'text-secondary'

  return (
    <div className="bg-surface-light border border-primary/10 rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className={clsx('text-sm font-semibold font-mono uppercase tracking-wider', accent)}>
          {label}
        </h2>
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className={clsx('flex items-center gap-1 text-[11px] hover:underline', accent)}
          >
            <ExternalLink className="w-3 h-3" /> open
          </a>
        )}
      </div>

      {/* Title */}
      <div className="space-y-1.5">
        <label
          htmlFor={`${platform}-title`}
          className="text-[11px] font-mono uppercase tracking-wider text-gray-500"
        >
          Title
        </label>
        <input
          id={`${platform}-title`}
          value={fields.title}
          disabled={titleLocked}
          onChange={(e) => onChange({ ...fields, title: e.target.value })}
          className="w-full px-3 py-2 bg-surface border border-white/10 rounded-lg text-sm text-gray-200 focus:border-primary/40 focus:outline-none transition-colors disabled:opacity-50 disabled:cursor-not-allowed font-mono"
        />
        {titleLocked && (
          <p className="flex items-center gap-1.5 text-[11px] text-gold font-mono">
            <Lock className="w-3 h-3" /> Title locked (keeper) — unlock to edit.
          </p>
        )}
      </div>

      {/* Description */}
      <div className="space-y-1.5">
        <label
          htmlFor={`${platform}-description`}
          className="text-[11px] font-mono uppercase tracking-wider text-gray-500"
        >
          Description
        </label>
        <textarea
          id={`${platform}-description`}
          value={fields.description}
          onChange={(e) => onChange({ ...fields, description: e.target.value })}
          rows={12}
          className="w-full px-3 py-2 bg-surface border border-white/10 rounded-lg text-xs text-gray-300 font-mono leading-relaxed focus:border-primary/40 focus:outline-none transition-colors resize-y"
        />
      </div>

      {/* Tags */}
      <div className="space-y-1.5">
        <label
          htmlFor={`${platform}-tags`}
          className="text-[11px] font-mono uppercase tracking-wider text-gray-500"
        >
          Tags
        </label>
        <TagsInput
          idPrefix={platform}
          tags={fields.tags}
          onChange={(tags) => onChange({ ...fields, tags })}
        />
      </div>
    </div>
  )
}

export default function CatalogMixEditor() {
  const { id = '' } = useParams()
  const { data: mix, isLoading } = useMix(id)
  const editMix = useEditCatalogMix(id)
  const lockTitle = useLockTitle(id)
  const proposals = useProposals({ mix_id: id, limit: 50 })

  const hasYoutube = !!(mix?.youtube_video_id || mix?.youtube_url)
  const hasSoundcloud = !!(mix?.soundcloud_track_id || mix?.soundcloud_url)

  const [yt, setYt] = useState<PlatformFields>(emptyFields)
  const [sc, setSc] = useState<PlatformFields>(emptyFields)
  const [base, setBase] = useState<{ yt: PlatformFields; sc: PlatformFields }>({
    yt: emptyFields,
    sc: emptyFields,
  })
  const [loadedId, setLoadedId] = useState<string | null>(null)

  // Initialize form state once per mix
  useEffect(() => {
    if (!mix || mix.id === loadedId) return
    const ytFields: PlatformFields = {
      title: mix.title_youtube ?? mix.title,
      description: mix.description_youtube ?? '',
      tags: mix.tags ?? [],
    }
    const scFields: PlatformFields = {
      title: mix.title,
      description: mix.description_soundcloud ?? '',
      tags: mix.tags ?? [],
    }
    setYt(ytFields)
    setSc(scFields)
    setBase({ yt: ytFields, sc: scFields })
    setLoadedId(mix.id)
  }, [mix, loadedId])

  const buildPayload = (apply: boolean): CatalogMixEditBody | null => {
    const body: CatalogMixEditBody = { apply }
    if (hasYoutube) {
      const diff = platformDiff(yt, base.yt)
      if (diff) body.youtube = diff
    }
    if (hasSoundcloud) {
      const diff = platformDiff(sc, base.sc)
      if (diff) body.soundcloud = diff
    }
    return body.youtube || body.soundcloud ? body : null
  }

  const dirty = useMemo(
    () =>
      (hasYoutube && platformDiff(yt, base.yt) !== null) ||
      (hasSoundcloud && platformDiff(sc, base.sc) !== null),
    [hasYoutube, hasSoundcloud, yt, sc, base],
  )

  const save = (apply: boolean) => {
    const body = buildPayload(apply)
    if (!body) return
    editMix.mutate(body, {
      onSuccess: (res) => {
        toast.success(
          `${res.proposals.length} proposal${res.proposals.length === 1 ? '' : 's'} saved${
            res.applying ? ' — applying now' : ''
          }`,
        )
        setBase({ yt, sc })
      },
      onError: () => toast.error('Failed to save edits'),
    })
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-500 font-mono py-20 justify-center">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading mix…
      </div>
    )
  }

  if (!mix) {
    return (
      <div className="py-20 text-center text-gray-600">
        <p className="font-pixel text-xs">Mix not found</p>
        <Link to="/catalog" className="text-primary text-sm hover:underline mt-3 inline-block">
          Back to catalog
        </Link>
      </div>
    )
  }

  const catalogMeta = ((mix.metadata_json as Record<string, unknown> | null)?.catalog ?? {}) as {
    youtube?: { thumbnail_url?: string }
    soundcloud?: { artwork_url?: string }
  }
  const thumbnail = catalogMeta.youtube?.thumbnail_url || catalogMeta.soundcloud?.artwork_url
  const locked = !!mix.title_locked
  const proposalItems = proposals.data?.items ?? []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            to="/catalog"
            className="flex items-center gap-1.5 text-[11px] text-gray-500 hover:text-primary font-mono transition-colors mb-2"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> Catalog
          </Link>
          <div className="flex items-center gap-3">
            <h1 className="font-pixel text-base text-primary glow-text truncate">{mix.title}</h1>
            <button
              onClick={() =>
                lockTitle.mutate(!locked, {
                  onSuccess: (res) =>
                    toast.success(res.title_locked ? 'Title locked (keeper)' : 'Title unlocked'),
                  onError: () => toast.error('Failed to toggle title lock'),
                })
              }
              disabled={lockTitle.isPending}
              title={locked ? 'Unlock title' : 'Lock title (keeper — AI never proposes changes)'}
              className={clsx(
                'p-1.5 rounded-lg border transition-colors shrink-0',
                locked
                  ? 'text-gold border-gold/40 bg-gold/10 hover:bg-gold/20'
                  : 'text-gray-500 border-white/10 hover:text-gray-300 hover:border-white/20',
              )}
            >
              {locked ? <Lock className="w-4 h-4" /> : <Unlock className="w-4 h-4" />}
            </button>
          </div>
          {mix.source === 'imported' && (
            <span className="inline-block mt-2 text-[10px] px-2 py-0.5 rounded-full font-mono uppercase tracking-wider text-accent border border-accent/40 bg-accent/10">
              imported
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => save(false)}
            disabled={!dirty || editMix.isPending}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary/10 text-primary border border-primary/30 text-xs font-mono uppercase tracking-wider hover:bg-primary/20 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
          >
            {editMix.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Save
          </button>
          <button
            onClick={() => save(true)}
            disabled={!dirty || editMix.isPending}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyber-cyan/10 text-cyber-cyan border border-cyber-cyan/30 text-xs font-mono uppercase tracking-wider hover:bg-cyber-cyan/20 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
          >
            <Send className="w-4 h-4" /> Save &amp; Apply
          </button>
        </div>
      </div>

      {/* Platform panels */}
      <div className={clsx('grid gap-5', hasYoutube && hasSoundcloud ? 'lg:grid-cols-2' : 'grid-cols-1')}>
        {hasYoutube && (
          <PlatformPanel
            platform="youtube"
            fields={yt}
            onChange={setYt}
            titleLocked={locked}
            url={mix.youtube_url}
          />
        )}
        {hasSoundcloud && (
          <PlatformPanel
            platform="soundcloud"
            fields={sc}
            onChange={setSc}
            titleLocked={locked}
            url={mix.soundcloud_url}
          />
        )}
        {!hasYoutube && !hasSoundcloud && (
          <div className="bg-surface-light border border-primary/10 rounded-xl p-8 text-center text-gray-600 text-sm">
            This mix has no linked platform yet — run the pipeline or a catalog sync first.
          </div>
        )}
      </div>

      {/* Thumbnail / artwork */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-5 space-y-3">
        <h2 className="text-sm font-semibold font-mono uppercase tracking-wider text-gray-300 flex items-center gap-2">
          <ImageIcon className="w-4 h-4 text-primary" /> Thumbnail / Artwork
        </h2>
        <div className="flex flex-wrap items-start gap-4">
          {thumbnail ? (
            <img
              src={thumbnail}
              alt="Current thumbnail"
              className="w-64 max-w-full rounded-lg border border-white/10"
            />
          ) : (
            <div className="w-64 aspect-video rounded-lg bg-dark flex items-center justify-center">
              <Music className="w-10 h-10 text-primary/30" />
            </div>
          )}
          <p className="flex-1 min-w-[220px] flex items-start gap-2 text-[11px] text-gray-500 font-mono leading-relaxed">
            <Info className="w-3.5 h-3.5 mt-0.5 shrink-0 text-accent" />
            <span>
              Thumbnail changes are managed as proposals in the{' '}
              <Link to={`/catalog/review?mix_id=${mix.id}`} className="text-primary hover:underline">
                review queue
              </Link>
              . Direct file upload / AI regenerate isn&apos;t wired up yet (TODO: needs a backend
              upload endpoint).
            </span>
          </p>
        </div>
      </div>

      {/* Existing proposals */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold font-mono uppercase tracking-wider text-gray-300 flex items-center gap-2">
            <ListChecks className="w-4 h-4 text-primary" /> Proposals
          </h2>
          <Link
            to={`/catalog/review?mix_id=${mix.id}`}
            className="text-[11px] text-primary hover:underline font-mono"
          >
            open in review queue
          </Link>
        </div>
        {proposalItems.length === 0 ? (
          <p className="text-xs text-gray-600 font-mono">No proposals for this mix yet.</p>
        ) : (
          <div className="space-y-2">
            {proposalItems.map((p: Proposal) => (
              <div
                key={p.id}
                className="flex flex-wrap items-center gap-2 text-xs border-b border-white/5 pb-2 last:border-0 last:pb-0"
              >
                <ProposalStatusChip status={p.status} />
                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-dark text-gray-500">
                  {p.platform}
                </span>
                <span className="text-gray-300 font-mono">{p.field}</span>
                <span
                  className={clsx(
                    'text-[10px] font-mono px-1.5 py-0.5 rounded',
                    p.created_by === 'ai'
                      ? 'bg-cyber-magenta/10 text-cyber-magenta'
                      : 'bg-primary/10 text-primary',
                  )}
                >
                  {p.created_by}
                </span>
                {p.created_at && (
                  <span className="text-[10px] text-gray-600 font-mono ml-auto">
                    {formatDistanceToNow(new Date(p.created_at), { addSuffix: true })}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
