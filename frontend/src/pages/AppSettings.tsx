import { useState, useEffect } from 'react'
import {
  Settings,
  Save,
  FolderOpen,
  Upload,
  Brain,
  Bell,
  Shield,
  Plus,
  X,
  CheckCircle2,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { useSettings, useUpdateSettings } from '@/api/hooks'

function Toggle({
  checked,
  onChange,
  label,
  description,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
  description?: string
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-sm text-gray-300">{label}</p>
        {description && <p className="text-[11px] text-gray-600 mt-0.5">{description}</p>}
      </div>
      <button
        onClick={() => onChange(!checked)}
        className={clsx(
          'relative w-11 h-6 rounded-full transition-colors shrink-0',
          checked ? 'bg-primary' : 'bg-gray-700',
        )}
      >
        <span
          className={clsx(
            'absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform shadow',
            checked ? 'translate-x-[22px]' : 'translate-x-0.5',
          )}
        />
      </button>
    </div>
  )
}

export default function AppSettings() {
  const { data: settings, isLoading } = useSettings()
  const update = useUpdateSettings()

  const [form, setForm] = useState<{
    watch_paths: string[]
    upload_to_soundcloud: boolean
    upload_to_youtube: boolean
    draft_mode: boolean
    ai_provider: string
    ai_model: string
    notification_email: string
    notification_discord_webhook: string
  }>({
    watch_paths: [],
    upload_to_soundcloud: true,
    upload_to_youtube: false,
    draft_mode: true,
    ai_provider: 'openai',
    ai_model: 'gpt-4o',
    notification_email: '',
    notification_discord_webhook: '',
  })

  const [newPath, setNewPath] = useState('')

  useEffect(() => {
    if (settings) {
      setForm({
        watch_paths: settings.watch_paths,
        upload_to_soundcloud: settings.upload_to_soundcloud,
        upload_to_youtube: settings.upload_to_youtube,
        draft_mode: settings.draft_mode,
        ai_provider: settings.ai_provider,
        ai_model: settings.ai_model,
        notification_email: settings.notification_email ?? '',
        notification_discord_webhook: settings.notification_discord_webhook ?? '',
      })
    }
  }, [settings])

  const handleSave = () => {
    update.mutate(form, {
      onSuccess: () => toast.success('Settings saved'),
      onError: () => toast.error('Failed to save settings'),
    })
  }

  const addPath = () => {
    if (!newPath.trim()) return
    setForm({ ...form, watch_paths: [...form.watch_paths, newPath.trim()] })
    setNewPath('')
  }

  const removePath = (index: number) => {
    setForm({ ...form, watch_paths: form.watch_paths.filter((_, i) => i !== index) })
  }

  if (isLoading) {
    return <div className="h-96 bg-surface-light rounded-xl animate-pulse" />
  }

  return (
    <div className="space-y-8 max-w-3xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
            <Settings className="w-6 h-6" /> Settings
          </h1>
          <p className="text-sm text-gray-500 mt-1">Configure automation behavior</p>
        </div>
        <button
          onClick={handleSave}
          disabled={update.isPending}
          className="flex items-center gap-2 px-5 py-2.5 bg-primary text-dark font-semibold rounded-lg text-sm hover:bg-primary/90 disabled:opacity-50 transition-all"
        >
          <Save className="w-4 h-4" />
          {update.isPending ? 'Saving...' : 'Save'}
        </button>
      </div>

      {/* Watch Paths */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <FolderOpen className="w-4 h-4" /> Watch Paths
        </h2>
        <p className="text-[11px] text-gray-600">Directories to monitor for new audio files</p>
        <div className="space-y-2">
          {form.watch_paths.map((p, i) => (
            <div key={i} className="flex items-center gap-2 group">
              <code className="flex-1 px-3 py-2 bg-dark rounded-lg text-sm text-gray-400 font-mono truncate">
                {p}
              </code>
              <button
                onClick={() => removePath(i)}
                className="p-2 text-gray-600 hover:text-cyber-red transition-colors opacity-0 group-hover:opacity-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addPath()}
            placeholder="/path/to/mixes"
            className="flex-1 px-4 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
          />
          <button
            onClick={addPath}
            className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg hover:bg-primary/20 transition-all"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Upload Settings */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <Upload className="w-4 h-4" /> Upload Settings
        </h2>
        <div className="space-y-4">
          <Toggle
            checked={form.draft_mode}
            onChange={(v) => setForm({ ...form, draft_mode: v })}
            label="Draft Mode"
            description="Require manual approval before uploading to platforms"
          />
          <div className="neon-line" />
          <Toggle
            checked={form.upload_to_soundcloud}
            onChange={(v) => setForm({ ...form, upload_to_soundcloud: v })}
            label="Upload to SoundCloud"
            description={settings?.soundcloud_connected ? 'Connected' : 'Not connected - authorize below'}
          />
          <div className="flex items-center gap-2 pl-4">
            {settings?.soundcloud_connected ? (
              <span className="flex items-center gap-1.5 text-xs text-primary">
                <CheckCircle2 className="w-3.5 h-3.5" /> Authorized
              </span>
            ) : (
              <button className="text-xs text-secondary hover:text-secondary/80 underline transition-colors">
                Connect SoundCloud
              </button>
            )}
          </div>
          <div className="neon-line" />
          <Toggle
            checked={form.upload_to_youtube}
            onChange={(v) => setForm({ ...form, upload_to_youtube: v })}
            label="Upload to YouTube"
            description={settings?.youtube_connected ? 'Connected' : 'Not connected - authorize below'}
          />
          <div className="flex items-center gap-2 pl-4">
            {settings?.youtube_connected ? (
              <span className="flex items-center gap-1.5 text-xs text-primary">
                <CheckCircle2 className="w-3.5 h-3.5" /> Authorized
              </span>
            ) : (
              <button className="text-xs text-cyber-red hover:text-cyber-red/80 underline transition-colors">
                Connect YouTube
              </button>
            )}
          </div>
        </div>
      </div>

      {/* AI Provider */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <Brain className="w-4 h-4" /> AI Provider
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Provider</label>
            <select
              value={form.ai_provider}
              onChange={(e) => setForm({ ...form, ai_provider: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
            >
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="local">Local (Ollama)</option>
            </select>
          </div>
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Model</label>
            <input
              type="text"
              value={form.ai_model}
              onChange={(e) => setForm({ ...form, ai_model: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              placeholder="gpt-4o"
            />
          </div>
        </div>
      </div>

      {/* Notifications */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <Bell className="w-4 h-4" /> Notifications
        </h2>
        <div className="space-y-4">
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Email</label>
            <input
              type="email"
              value={form.notification_email}
              onChange={(e) => setForm({ ...form, notification_email: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              placeholder="you@example.com"
            />
          </div>
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">
              Discord Webhook URL
            </label>
            <input
              type="url"
              value={form.notification_discord_webhook}
              onChange={(e) => setForm({ ...form, notification_discord_webhook: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              placeholder="https://discord.com/api/webhooks/..."
            />
          </div>
        </div>
      </div>

      {/* Security note */}
      <div className="flex items-start gap-3 p-4 bg-gold/5 border border-gold/20 rounded-xl">
        <Shield className="w-5 h-5 text-gold shrink-0 mt-0.5" />
        <div>
          <p className="text-sm text-gold">API keys are stored server-side</p>
          <p className="text-[11px] text-gray-500 mt-1">
            Platform credentials and API keys are managed through environment variables on the backend.
            They are never exposed through this dashboard.
          </p>
        </div>
      </div>
    </div>
  )
}
