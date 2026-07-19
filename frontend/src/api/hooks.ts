import { useEffect, useSyncExternalStore } from 'react'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import client from './client'
import { wsManager, type WsMessage } from './ws'

// ---------- Types (matching actual backend API responses) ----------

export interface Mix {
  id: string
  title: string
  source?: 'pipeline' | 'imported' | string
  youtube_video_id?: string | null
  soundcloud_track_id?: string | null
  title_locked?: boolean
  audio_file_path: string | null
  video_file_path: string | null
  duration_seconds: number | null
  genres: string[] | null
  vibes: string[] | null
  energy_profile: { time: number; rms: number }[] | null
  tracklist: { title: string; artist: string; timestamp_seconds: number; timestamp_formatted?: string }[] | null
  description_soundcloud: string | null
  description_youtube: string | null
  title_youtube: string | null
  tags: string[] | null
  cover_art_path: string | null
  thumbnail_path: string | null
  soundcloud_url: string | null
  youtube_url: string | null
  youtube_playlist_id: string | null
  youtube_timestamp_offset: number
  pipeline_status: string
  pipeline_step: string | null
  pipeline_error: string | null
  pipeline_started_at: string | null
  pipeline_completed_at: string | null
  created_at: string
  updated_at: string
  metadata_json: Record<string, unknown> | null
}

export interface MixDetail extends Mix {
  steps: PipelineStep[]
}

export type PipelineStepStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'waiting'
  | 'interrupted'

export interface PipelineStep {
  id: number
  mix_id: string
  step_name: string
  status: PipelineStepStatus | string
  started_at: string | null
  completed_at: string | null
  error: string | null
  retry_count: number
  progress: number | null
  progress_detail: string | null
  output_json: Record<string, unknown> | null
}

export interface PipelineStatus {
  paused: boolean
  active: number
  queued: number
  completed: number
  failed: number
}

export interface AppSettings {
  id: number
  image_gen_provider: string
  image_gen_model: string
  llm_provider: string
  llm_model: string
  premiere_mode: string
  premiere_hour_utc: number
  premiere_day: string
  draft_mode: boolean
  auto_upgrade: boolean
  settings_json: Record<string, unknown> | null
  updated_at: string
}

export interface BrandSettings {
  id: number
  brand_name: string | null
  description_template: string | null
  color_palette: string[] | null
  visual_style: string | null
  motifs: string[] | null
  genre_visual_modifiers: Record<string, string> | null
  title_format: string | null
  youtube_playlists: Record<string, string> | null
  soundcloud_links: string | null
  youtube_links: string | null
  updated_at: string | null
}

export interface AIUsageRecord {
  id: number
  mix_id: string | null
  provider: string
  model: string
  operation: string
  input_tokens: number
  output_tokens: number
  cost_usd: number
  created_at: string
}

export interface AIBudget {
  monthly_budget: number
  spent_this_month: number
  remaining: number
  percentage_used: number
}

export interface NotificationRecord {
  id: number
  mix_id: string | null
  type: string
  channel: string
  message: string
  sent: boolean
  sent_at: string | null
  created_at: string
}

export interface UpgradeStatus {
  current_version: string
  latest_version: string | null
  update_available: boolean
  last_checked: string | null
}

export interface BackupItem {
  filename: string
  path: string
  size_bytes: number
  created_at: string
}

// ---------- Paginated response wrapper ----------

interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

// ---------- WebSocket bridge ----------

/** True while the live WebSocket is connected; false = polling fallback. */
export function useWsConnected(): boolean {
  return useSyncExternalStore(
    (cb) => wsManager.subscribeStatus(() => cb()),
    () => wsManager.connected,
  )
}

const PIPELINE_EVENTS = new Set([
  'step_completed',
  'pipeline_started',
  'upload_complete',
  'error',
  'draft_ready',
  'snapshot',
])

/**
 * Bridges WS events into react-query caches. Mount once (in Layout).
 * - step_progress: optimistically patch cached mix detail + list rows
 * - pipeline lifecycle events: invalidate mixes + pipeline status
 * - activity: invalidate activity caches
 */
export function useLiveEvents() {
  const qc = useQueryClient()

  useEffect(() => {
    const unsubs = [
      wsManager.subscribe('step_progress', (msg: WsMessage) => {
        if (!msg.mix_id) return
        const { step, progress, detail } = msg.data as {
          step?: string
          progress?: number | null
          detail?: string | null
        }
        if (!step) return
        // Patch mix detail cache
        qc.setQueryData<MixDetail>(['mixes', msg.mix_id], (old) => {
          if (!old) return old
          return {
            ...old,
            pipeline_step: step,
            steps: (old.steps ?? []).map((s) =>
              s.step_name === step
                ? {
                    ...s,
                    status: 'running',
                    progress: progress ?? null,
                    progress_detail: detail ?? null,
                  }
                : s,
            ),
          }
        })
        // Patch any cached mix lists
        qc.setQueriesData<Paginated<Mix>>({ queryKey: ['mixes'] }, (old) => {
          if (!old || !Array.isArray((old as Paginated<Mix>).items)) return old
          return {
            ...old,
            items: old.items.map((m) =>
              m.id === msg.mix_id ? { ...m, pipeline_step: step } : m,
            ),
          }
        })
      }),
      wsManager.subscribe('*', (msg: WsMessage) => {
        if (!PIPELINE_EVENTS.has(msg.event)) return
        qc.invalidateQueries({ queryKey: ['mixes'] })
        qc.invalidateQueries({ queryKey: ['pipeline'] })
        if (msg.mix_id) qc.invalidateQueries({ queryKey: ['mixes', msg.mix_id] })
      }),
      wsManager.subscribe('activity', (msg: WsMessage) => {
        // Invalidate plain activity queries (dashboard card). The Activity page's
        // infinite query handles live events itself via its own WS subscription.
        qc.invalidateQueries({
          predicate: (q) => q.queryKey[0] === 'activity' && q.queryKey[1] !== 'infinite',
        })
        // Catalog events (catalog_sync / catalog_apply / catalog_improve) mean
        // mixes, proposals, or sync state changed — refresh catalog caches.
        const ev = (msg.data as { event?: string | null } | undefined)?.event
        if (typeof ev === 'string' && ev.startsWith('catalog')) {
          qc.invalidateQueries({ queryKey: ['catalog'] })
        }
      }),
    ]
    return () => unsubs.forEach((u) => u())
  }, [qc])
}

// ---------- Mixes ----------

export type MixSort = 'newest' | 'oldest' | 'title'
export type CatalogSort = MixSort | 'duration'

export function useMixes(params?: {
  status?: string
  page?: number
  page_size?: number
  sort?: MixSort
}) {
  const wsConnected = useWsConnected()
  return useQuery({
    queryKey: ['mixes', params],
    queryFn: () => client.get<Paginated<Mix>>('/mixes', { params }).then((r) => r.data),
    // Polling fallback when the live socket is down
    refetchInterval: wsConnected ? false : 5000,
  })
}

export function useMix(id: string) {
  const wsConnected = useWsConnected()
  return useQuery({
    queryKey: ['mixes', id],
    queryFn: () => client.get<MixDetail>(`/mixes/${id}`).then((r) => r.data),
    enabled: !!id,
    refetchInterval: wsConnected ? false : 5000,
  })
}

export function useCreateMix() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: { title: string; audio_file_path?: string; video_file_path?: string }) =>
      client.post('/mixes', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mixes'] })
    },
  })
}

export function useApproveMix() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.post(`/mixes/${id}/approve`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mixes'] })
    },
  })
}

export function useRetryMix() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.post(`/mixes/${id}/retry`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mixes'] })
    },
  })
}

export function useRetryStep(mixId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (step: string) => client.post(`/mixes/${mixId}/retry-step/${step}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mixes'] })
    },
  })
}

export function useRereadTracklist() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.post(`/mixes/${id}/reread-tracklist`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mixes'] })
    },
  })
}

export function useDeleteMix() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.delete(`/mixes/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mixes'] })
    },
  })
}

// ---------- Pipeline ----------

export function usePipelineStatus() {
  return useQuery({
    queryKey: ['pipeline'],
    queryFn: () => client.get<PipelineStatus>('/pipeline/status').then((r) => r.data),
    refetchInterval: 5000,
  })
}

export function usePausePipeline() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/pipeline/pause'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pipeline'] })
    },
  })
}

export function useResumePipeline() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/pipeline/resume'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pipeline'] })
    },
  })
}

// ---------- Settings ----------

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: () => client.get<AppSettings>('/settings').then((r) => r.data),
  })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<AppSettings>) => client.put('/settings', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })
}

// ---------- Brand ----------

export function useBrand() {
  return useQuery({
    queryKey: ['brand'],
    queryFn: () => client.get<BrandSettings>('/brand').then((r) => r.data),
  })
}

export function useUpdateBrand() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<BrandSettings>) => client.put('/brand', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['brand'] })
    },
  })
}

// ---------- AI Usage ----------

export function useAIUsage(params?: { start_date?: string; end_date?: string; page?: number }) {
  return useQuery({
    queryKey: ['ai-usage', params],
    queryFn: () => client.get<Paginated<AIUsageRecord>>('/ai/usage', { params }).then((r) => r.data),
  })
}

export function useAIUsageSummary() {
  return useQuery({
    queryKey: ['ai-usage-summary'],
    queryFn: () => client.get<{ items: { provider: string; model: string; operation: string; total_cost: number; total_input_tokens: number; total_output_tokens: number; count: number }[] }>('/ai/usage/summary').then((r) => r.data),
  })
}

export function useAIBudget() {
  return useQuery({
    queryKey: ['ai-budget'],
    queryFn: () => client.get<AIBudget>('/ai/budget').then((r) => r.data),
  })
}

// ---------- Notifications ----------

export function useNotifications(params?: { type?: string; channel?: string; page?: number }) {
  return useQuery({
    queryKey: ['notifications', params],
    queryFn: () => client.get<Paginated<NotificationRecord>>('/notifications', { params }).then((r) => r.data),
  })
}

export type NotificationChannel = 'discord' | 'email' | 'webhook'

export interface NotificationTestResult {
  success: boolean
  channel: string
  detail: string | null
}

export function useTestNotification() {
  return useMutation({
    mutationFn: (channel: NotificationChannel) =>
      client.post<NotificationTestResult>(`/notifications/test/${channel}`).then((r) => r.data),
  })
}

export interface NotificationSettings {
  discord_webhook_url: string | null
  email_smtp_host: string | null
  email_smtp_port: number | null
  email_smtp_user: string | null
  has_password: boolean
  email_from: string | null
  email_to: string | null
  email_smtp_secure: boolean
  webhook_urls: string[]
  events: Record<string, boolean>
  min_level: 'info' | 'warn' | 'error'
}

export type NotificationSettingsUpdate = Partial<
  Omit<NotificationSettings, 'has_password'> & { email_smtp_password: string }
>

export function useNotificationSettings() {
  return useQuery({
    queryKey: ['notification-settings'],
    queryFn: () =>
      client.get<NotificationSettings>('/notifications/settings').then((r) => r.data),
  })
}

export function useUpdateNotificationSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: NotificationSettingsUpdate) => client.put('/notifications/settings', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notification-settings'] })
    },
  })
}

// ---------- Activity log ----------

export interface ActivityItem {
  id: number
  ts: string
  level: 'info' | 'warn' | 'error'
  event: string | null
  message: string | null
  mix_id: string | null
  filename: string | null
  platform: string | null
  stage: string | null
  context: Record<string, unknown> | null
}

export interface ActivityResponse {
  items: ActivityItem[]
  total: number
  limit: number
  offset: number
}

export function useActivity(params?: { level?: string; mix_id?: string; limit?: number }) {
  const wsConnected = useWsConnected()
  return useQuery({
    queryKey: ['activity', params],
    queryFn: () =>
      client
        .get<ActivityResponse>('/activity', { params: { limit: 100, ...params } })
        .then((r) => r.data),
    // WS invalidation is the live path; poll only when degraded
    refetchInterval: wsConnected ? false : 5000,
  })
}

export interface ActivityFilters {
  level?: string
  mix_id?: string
  event?: string
  q?: string
  platform?: string
  since?: string
  until?: string
  limit?: number
}

export function useActivityInfinite(filters: ActivityFilters) {
  const limit = filters.limit ?? 50
  return useInfiniteQuery({
    queryKey: ['activity', 'infinite', filters],
    queryFn: ({ pageParam }) =>
      client
        .get<ActivityResponse>('/activity', {
          params: {
            ...filters,
            limit,
            before_id: pageParam === 0 ? undefined : pageParam,
          },
        })
        .then((r) => r.data),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      if (!lastPage.items.length || lastPage.items.length < limit) return undefined
      return lastPage.items[lastPage.items.length - 1].id
    },
  })
}

// ---------- Upgrade ----------

export function useUpgradeStatus() {
  return useQuery({
    queryKey: ['upgrade'],
    queryFn: () => client.get<UpgradeStatus>('/upgrade/status').then((r) => r.data),
  })
}

export function useCheckUpgrade() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/upgrade/check'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['upgrade'] })
    },
  })
}

export function useApplyUpgrade() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/upgrade/apply'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['upgrade'] })
    },
  })
}

export function useBackups() {
  return useQuery({
    queryKey: ['backups'],
    queryFn: () => client.get<{ backups: BackupItem[]; total: number }>('/upgrade/backups').then((r) => r.data),
  })
}

export function useCreateBackup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/upgrade/backup'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['backups'] })
    },
  })
}

// ---------- Catalog ----------

export interface CatalogMix {
  id: string
  title: string
  source: 'pipeline' | 'imported' | string
  platforms: ('youtube' | 'soundcloud')[]
  youtube_video_id: string | null
  soundcloud_track_id: string | null
  youtube_url: string | null
  soundcloud_url: string | null
  thumbnail_url: string | null
  artwork_url: string | null
  duration_seconds: number | null
  youtube_published_at: string | null
  soundcloud_published_at: string | null
  title_locked: boolean
  open_proposals: number
}

export interface CatalogSyncSummary {
  started_at: string
  finished_at?: string
  status: 'ok' | 'partial' | 'failed' | 'running' | string
  errors: string[]
  youtube_items?: number
  soundcloud_items?: number
  matched_pairs?: number
  ambiguous_judged?: number
  singles?: number
  existing_updated?: number
  pairs_created?: number
  singles_created?: number
}

export interface CatalogSyncStatus {
  running: boolean
  last_sync: CatalogSyncSummary | null
}

export type ProposalStatus = 'draft' | 'approved' | 'rejected' | 'applying' | 'applied' | 'failed'
export type ProposalField = 'title' | 'description' | 'thumbnail' | 'playlist' | 'tags'
export type ProposalPlatform = 'youtube' | 'soundcloud' | 'both'

export interface Proposal {
  id: string
  mix_id: string
  mix_title: string | null
  platform: ProposalPlatform
  field: ProposalField
  current_value: string | null
  proposed_value: string | null
  status: ProposalStatus
  created_by: 'ai' | 'user'
  error: string | null
  created_at: string | null
  updated_at: string | null
  applied_at: string | null
}

export interface CatalogMixesParams {
  page?: number
  page_size?: number
  platform?: 'yt-only' | 'sc-only' | 'both'
  source?: string
  q?: string
  sort?: CatalogSort
}

export function useCatalogMixes(params?: CatalogMixesParams) {
  return useQuery({
    queryKey: ['catalog', 'mixes', params],
    queryFn: () =>
      client.get<Paginated<CatalogMix>>('/catalog/mixes', { params }).then((r) => r.data),
  })
}

export function useCatalogSyncStatus() {
  return useQuery({
    queryKey: ['catalog', 'sync-status'],
    queryFn: () => client.get<CatalogSyncStatus>('/catalog/sync/status').then((r) => r.data),
    // Poll while a sync is in flight so the UI notices completion
    refetchInterval: (query) => (query.state.data?.running ? 2_000 : false),
  })
}

export function useStartCatalogSync() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.post<{ status: 'started' | 'already_running' }>('/catalog/sync').then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog', 'sync-status'] })
    },
  })
}

export interface CatalogBackfillSummary {
  started_at: string
  finished_at?: string
  status: 'ok' | 'partial' | 'failed' | 'cancelled' | 'running' | string
  errors: string[]
  eligible_mixes?: number
  audio_files?: number
  matched?: number
  processed?: number
  tracks_found?: number
  proposals_created?: number
  failed?: number
  cancelled?: boolean
  unmatched_files?: number
  unmatched_mixes?: { mix_id: string; title: string }[]
}

export interface CatalogBackfillStatus {
  running: boolean
  last_backfill: CatalogBackfillSummary | null
}

export function useCatalogBackfillStatus() {
  return useQuery({
    queryKey: ['catalog', 'backfill-status'],
    queryFn: () =>
      client.get<CatalogBackfillStatus>('/catalog/backfill-status').then((r) => r.data),
    // Poll while a backfill is in flight so the UI notices completion
    refetchInterval: (query) => (query.state.data?.running ? 2_000 : false),
  })
}

export function useStartCatalogBackfill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (mixIds?: string[]) =>
      client
        .post<{ status: 'started' | 'already_running' }>('/catalog/backfill-tracklists', {
          mix_ids: mixIds ?? null,
        })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog', 'backfill-status'] })
    },
  })
}

export interface PlatformEdit {
  title?: string
  description?: string
  tags?: string[]
}

export interface CatalogMixEditBody {
  youtube?: PlatformEdit
  soundcloud?: PlatformEdit
  apply?: boolean
}

export function useEditCatalogMix(mixId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CatalogMixEditBody) =>
      client
        .put<{ proposals: Proposal[]; applying: boolean }>(`/catalog/mixes/${mixId}`, body)
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
      qc.invalidateQueries({ queryKey: ['mixes', mixId] })
    },
  })
}

export function useLockTitle(mixId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (locked: boolean) =>
      client
        .post<{ id: string; title_locked: boolean }>(`/catalog/mixes/${mixId}/lock-title`, { locked })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
      qc.invalidateQueries({ queryKey: ['mixes', mixId] })
    },
  })
}

export interface ProposalCreateBody {
  platform: ProposalPlatform
  field: ProposalField
  proposed_value: string
  current_value?: string | null
  status?: 'draft' | 'approved'
}

export function useCreateProposal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ mixId, ...body }: ProposalCreateBody & { mixId: string }) =>
      client.post<Proposal>(`/catalog/mixes/${mixId}/proposals`, body).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
    },
  })
}

export interface ProposalsParams {
  status?: string
  mix_id?: string
  limit?: number
  offset?: number
}

export function useProposals(params?: ProposalsParams) {
  return useQuery({
    queryKey: ['catalog', 'proposals', params],
    queryFn: () =>
      client
        .get<{ items: Proposal[]; total: number }>('/catalog/proposals', { params })
        .then((r) => r.data),
  })
}

export function useProposalAction() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'approve' | 'reject' }) =>
      client.post<Proposal>(`/catalog/proposals/${id}/${action}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
    },
  })
}

export function useApproveBulk() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (ids: string[]) =>
      client
        .post<{ approved: number; applying: boolean }>('/catalog/proposals/approve-bulk', { ids })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
    },
  })
}

export function useCatalogApply() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post<{ status: string }>('/catalog/apply').then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
    },
  })
}

export function useCatalogImprove() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (mixIds?: string[] | 'all_generic') =>
      client
        .post<{ status: string }>('/catalog/improve', { mix_ids: mixIds ?? 'all_generic' })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['catalog'] })
    },
  })
}
