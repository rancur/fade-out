import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import client from './client'

// ---------- Types ----------

export interface Mix {
  id: string
  title: string
  filename: string
  status: 'pending' | 'processing' | 'generating' | 'reviewing' | 'approved' | 'uploaded' | 'failed'
  genres: string[]
  duration_seconds: number
  bpm?: number
  cover_art_url?: string
  soundcloud_url?: string
  youtube_url?: string
  description?: string
  tracklist?: string[]
  energy_data?: { time: number; energy: number }[]
  pipeline_steps?: PipelineStep[]
  ai_cost?: number
  created_at: string
  updated_at: string
}

export interface PipelineStep {
  name: string
  status: 'pending' | 'running' | 'complete' | 'failed' | 'skipped'
  started_at?: string
  finished_at?: string
  error?: string
}

export interface PipelineStatus {
  queue_length: number
  active_mix_id?: string
  active_step?: string
  recent: { mix_id: string; title: string; status: string; finished_at: string }[]
}

export interface Settings {
  watch_paths: string[]
  upload_to_soundcloud: boolean
  upload_to_youtube: boolean
  soundcloud_connected: boolean
  youtube_connected: boolean
  draft_mode: boolean
  ai_provider: string
  ai_model: string
  notification_email?: string
  notification_discord_webhook?: string
}

export interface Brand {
  artist_name: string
  primary_color: string
  secondary_color: string
  style_description: string
  description_template: string
  motifs: string[]
  links: { label: string; url: string }[]
}

export interface AIUsageData {
  total_cost: number
  budget_limit: number
  month: string
  daily_costs: { date: string; description: number; tracklist: number; tags: number; cover: number }[]
  per_mix: { mix_id: string; title: string; cost: number; date: string }[]
}

export interface Notification {
  id: string
  type: 'success' | 'error' | 'warning' | 'info'
  title: string
  message: string
  mix_id?: string
  created_at: string
  read: boolean
}

export interface UpgradeStatus {
  current_version: string
  latest_version: string
  update_available: boolean
  changelog?: string
  backups: { name: string; date: string; size_mb: number }[]
}

// ---------- Mixes ----------

export function useMixes(params?: { status?: string; genre?: string; search?: string; page?: number; limit?: number }) {
  return useQuery({
    queryKey: ['mixes', params],
    queryFn: () => client.get<{ mixes: Mix[]; total: number }>('/mixes', { params }).then((r) => r.data),
  })
}

export function useMix(id: string) {
  return useQuery({
    queryKey: ['mixes', id],
    queryFn: () => client.get<Mix>(`/mixes/${id}`).then((r) => r.data),
    enabled: !!id,
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

// ---------- Settings ----------

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: () => client.get<Settings>('/settings').then((r) => r.data),
  })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<Settings>) => client.put('/settings', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })
}

// ---------- Brand ----------

export function useBrand() {
  return useQuery({
    queryKey: ['brand'],
    queryFn: () => client.get<Brand>('/brand').then((r) => r.data),
  })
}

export function useUpdateBrand() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<Brand>) => client.put('/brand', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['brand'] })
    },
  })
}

// ---------- AI Usage ----------

export function useAIUsage() {
  return useQuery({
    queryKey: ['ai-usage'],
    queryFn: () => client.get<AIUsageData>('/ai/usage').then((r) => r.data),
  })
}

export function useAIBudget() {
  return useQuery({
    queryKey: ['ai-budget'],
    queryFn: () => client.get<{ budget_limit: number; spent: number }>('/ai/budget').then((r) => r.data),
  })
}

export function useUpdateAIBudget() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (budget_limit: number) => client.put('/ai/budget', { budget_limit }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ai-budget'] })
      qc.invalidateQueries({ queryKey: ['ai-usage'] })
    },
  })
}

// ---------- Notifications ----------

export function useNotifications(params?: { type?: string; limit?: number }) {
  return useQuery({
    queryKey: ['notifications', params],
    queryFn: () => client.get<Notification[]>('/notifications', { params }).then((r) => r.data),
  })
}

export function useMarkNotificationRead() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.post(`/notifications/${id}/read`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notifications'] })
    },
  })
}

export function useTestNotification() {
  return useMutation({
    mutationFn: (channel: 'email' | 'discord') => client.post('/notifications/test', { channel }),
  })
}

// ---------- Upgrade ----------

export function useUpgradeStatus() {
  return useQuery({
    queryKey: ['upgrade'],
    queryFn: () => client.get<UpgradeStatus>('/upgrade/status').then((r) => r.data),
  })
}

export function usePerformUpgrade() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/upgrade/apply'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['upgrade'] })
    },
  })
}

export function useCreateBackup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => client.post('/upgrade/backup'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['upgrade'] })
    },
  })
}
