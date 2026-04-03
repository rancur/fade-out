import { useState, useEffect } from 'react'
import {
  Settings,
  Save,
  Brain,
  Image,
  Calendar,
  Shield,
  ToggleLeft,
} from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { useSettings, useUpdateSettings } from '@/api/hooks'
import OAuthConnect from '@/components/OAuthConnect'

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

  const [form, setForm] = useState({
    image_gen_provider: 'openai',
    image_gen_model: 'dall-e-3',
    llm_provider: 'openai',
    llm_model: 'gpt-4o',
    premiere_mode: 'immediate',
    premiere_hour_utc: 18,
    premiere_day: 'friday',
    draft_mode: true,
    auto_upgrade: false,
    settings_json: null as Record<string, unknown> | null,
  })

  useEffect(() => {
    if (settings) {
      setForm({
        image_gen_provider: settings.image_gen_provider,
        image_gen_model: settings.image_gen_model,
        llm_provider: settings.llm_provider,
        llm_model: settings.llm_model,
        premiere_mode: settings.premiere_mode,
        premiere_hour_utc: settings.premiere_hour_utc,
        premiere_day: settings.premiere_day,
        draft_mode: settings.draft_mode,
        auto_upgrade: settings.auto_upgrade,
        settings_json: settings.settings_json,
      })
    }
  }, [settings])

  const handleSave = () => {
    update.mutate(form, {
      onSuccess: () => toast.success('Settings saved'),
      onError: () => toast.error('Failed to save settings'),
    })
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

      {/* Platform Connections (OAuth) */}
      <OAuthConnect />

      {/* LLM Provider */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <Brain className="w-4 h-4" /> LLM Provider
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Provider</label>
            <select
              value={form.llm_provider}
              onChange={(e) => setForm({ ...form, llm_provider: e.target.value })}
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
              value={form.llm_model}
              onChange={(e) => setForm({ ...form, llm_model: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              placeholder="gpt-4o"
            />
          </div>
        </div>
      </div>

      {/* Image Generation */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <Image className="w-4 h-4" /> Image Generation
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Provider</label>
            <select
              value={form.image_gen_provider}
              onChange={(e) => setForm({ ...form, image_gen_provider: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
            >
              <option value="openai">OpenAI (DALL-E)</option>
              <option value="stability">Stability AI</option>
              <option value="midjourney">Midjourney</option>
            </select>
          </div>
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Model</label>
            <input
              type="text"
              value={form.image_gen_model}
              onChange={(e) => setForm({ ...form, image_gen_model: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              placeholder="dall-e-3"
            />
          </div>
        </div>
      </div>

      {/* Premiere Settings */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <Calendar className="w-4 h-4" /> Premiere Schedule
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Mode</label>
            <select
              value={form.premiere_mode}
              onChange={(e) => setForm({ ...form, premiere_mode: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
            >
              <option value="immediate">Immediate</option>
              <option value="scheduled">Scheduled</option>
              <option value="manual">Manual</option>
            </select>
          </div>
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Day</label>
            <select
              value={form.premiere_day}
              onChange={(e) => setForm({ ...form, premiere_day: e.target.value })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 appearance-none cursor-pointer focus:border-primary/40 focus:outline-none font-mono"
            >
              {['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'].map((d) => (
                <option key={d} value={d}>
                  {d.charAt(0).toUpperCase() + d.slice(1)}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">Hour (UTC)</label>
            <input
              type="number"
              min={0}
              max={23}
              value={form.premiere_hour_utc}
              onChange={(e) => setForm({ ...form, premiere_hour_utc: parseInt(e.target.value) || 0 })}
              className="w-full px-4 py-2.5 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
            />
          </div>
        </div>
      </div>

      {/* Toggles */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <ToggleLeft className="w-4 h-4" /> Behavior
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
            checked={form.auto_upgrade}
            onChange={(v) => setForm({ ...form, auto_upgrade: v })}
            label="Auto Upgrade"
            description="Automatically apply system updates when available"
          />
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
