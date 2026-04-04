import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import client from './client'

// ---------- Types (matching actual backend API responses) ----------

export interface Mix {
  id: string
  title: string
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

export interface PipelineStep {
  id: number
  mix_id: string
  step_name: string
  status: string
  started_at: string | null
  completed_at: string | null
  error: string | null
  retry_count: number
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

// ---------- Mixes ----------

export function useMixes(params?: { status?: string; page?: number; page_size?: number }) {
  return useQuery({
    queryKey: ['mixes', params],
    queryFn: () => client.get<Paginated<Mix>>('/mixes', { params }).then((r) => r.data),
  })
}

export function useMix(id: string) {
  return useQuery({
    queryKey: ['mixes', id],
    queryFn: () => client.get<MixDetail>(`/mixes/${id}`).then((r) => r.data),
    enabled: !!id,
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

export function useTestNotification() {
  return useMutation({
    mutationFn: (channel: string) => client.post('/notifications/test', { channel }),
  })
}

export function useNotificationSettings() {
  return useQuery({
    queryKey: ['notification-settings'],
    queryFn: () => client.get<Record<string, unknown>>('/notifications/settings').then((r) => r.data),
  })
}

export function useUpdateNotificationSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: Record<string, unknown>) => client.put('/notifications/settings', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notification-settings'] })
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
