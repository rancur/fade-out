import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import { toast } from 'sonner'
import {
  Bell,
  History,
  Loader2,
  Mail,
  MessageSquare,
  Save,
  Send,
  SlidersHorizontal,
} from 'lucide-react'
import {
  useNotificationSettings,
  useUpdateNotificationSettings,
  useTestNotification,
  type NotificationChannel,
  type NotificationSettings,
  type NotificationSettingsUpdate,
} from '@/api/hooks'
import NotificationHistoryList from './NotificationHistory'

const EVENT_LABELS: Record<string, string> = {
  pipeline_started: 'Pipeline started',
  step_completed: 'Step completed',
  upload_complete: 'Upload complete',
  error: 'Errors',
  draft_ready: 'Draft ready for review',
  upgrade_available: 'Upgrade available',
}

const MIN_LEVELS = ['info', 'warn', 'error'] as const

interface FormState {
  discord_webhook_url: string
  email_smtp_host: string
  email_smtp_port: string
  email_smtp_user: string
  email_smtp_password: string
  email_from: string
  email_to: string
  email_smtp_secure: boolean
  events: Record<string, boolean>
  min_level: 'info' | 'warn' | 'error'
}

function toForm(s: NotificationSettings): FormState {
  return {
    discord_webhook_url: s.discord_webhook_url ?? '',
    email_smtp_host: s.email_smtp_host ?? '',
    email_smtp_port: s.email_smtp_port != null ? String(s.email_smtp_port) : '',
    email_smtp_user: s.email_smtp_user ?? '',
    email_smtp_password: '',
    email_from: s.email_from ?? '',
    email_to: s.email_to ?? '',
    email_smtp_secure: s.email_smtp_secure,
    events: { ...s.events },
    min_level: s.min_level,
  }
}

const inputCls =
  'w-full bg-surface border border-white/10 rounded-lg px-3 py-2 text-sm font-mono text-gray-300 placeholder:text-gray-600 focus:outline-none focus:border-primary/40'

function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <label className="block">
      <span className="text-[10px] text-gray-500 font-mono uppercase tracking-wider">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  )
}

function SettingsTab() {
  const { data: settings, isLoading } = useNotificationSettings()
  const update = useUpdateNotificationSettings()
  const test = useTestNotification()
  const [form, setForm] = useState<FormState | null>(null)

  useEffect(() => {
    if (settings && !form) setForm(toForm(settings))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings])

  if (isLoading || !settings || !form) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-gray-500 font-mono">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading settings…
      </div>
    )
  }

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => (f ? { ...f, [key]: value } : f))

  // Only send fields that changed vs. server state
  const buildDirtyPayload = (): NotificationSettingsUpdate => {
    const payload: NotificationSettingsUpdate = {}
    if (form.discord_webhook_url !== (settings.discord_webhook_url ?? ''))
      payload.discord_webhook_url = form.discord_webhook_url || null
    if (form.email_smtp_host !== (settings.email_smtp_host ?? ''))
      payload.email_smtp_host = form.email_smtp_host || null
    const serverPort = settings.email_smtp_port != null ? String(settings.email_smtp_port) : ''
    if (form.email_smtp_port !== serverPort)
      payload.email_smtp_port = form.email_smtp_port ? Number(form.email_smtp_port) : null
    if (form.email_smtp_user !== (settings.email_smtp_user ?? ''))
      payload.email_smtp_user = form.email_smtp_user || null
    if (form.email_smtp_password) payload.email_smtp_password = form.email_smtp_password
    if (form.email_from !== (settings.email_from ?? '')) payload.email_from = form.email_from || null
    if (form.email_to !== (settings.email_to ?? '')) payload.email_to = form.email_to || null
    if (form.email_smtp_secure !== settings.email_smtp_secure)
      payload.email_smtp_secure = form.email_smtp_secure
    if (JSON.stringify(form.events) !== JSON.stringify(settings.events))
      payload.events = form.events
    if (form.min_level !== settings.min_level) payload.min_level = form.min_level
    return payload
  }

  const dirty = Object.keys(buildDirtyPayload()).length > 0

  const handleSave = () => {
    const payload = buildDirtyPayload()
    if (!Object.keys(payload).length) return
    update.mutate(payload, {
      onSuccess: () => {
        toast.success('Notification settings saved')
        setForm((f) => (f ? { ...f, email_smtp_password: '' } : f))
      },
      onError: () => toast.error('Failed to save notification settings'),
    })
  }

  const handleTest = (channel: NotificationChannel) => {
    test.mutate(channel, {
      onSuccess: (result) => {
        if (result.success) toast.success(result.detail || `Test ${channel} notification sent`)
        else toast.error(result.detail || `Test ${channel} notification failed`)
      },
      onError: () => toast.error(`Failed to send test ${channel} notification`),
    })
  }

  const eventKeys = Array.from(
    new Set([...Object.keys(EVENT_LABELS), ...Object.keys(form.events)]),
  )

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Discord card */}
        <div className="bg-surface-light border border-accent/10 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-accent/10">
            <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
              <MessageSquare className="w-4 h-4 text-accent" /> Discord
            </h2>
            <button
              onClick={() => handleTest('discord')}
              disabled={test.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-mono text-accent bg-accent/5 border border-accent/20 rounded-lg hover:bg-accent/15 disabled:opacity-50 transition-all"
            >
              <Send className="w-3 h-3" /> Test
            </button>
          </div>
          <div className="p-5">
            <Field label="Webhook URL">
              <input
                value={form.discord_webhook_url}
                onChange={(e) => set('discord_webhook_url', e.target.value)}
                placeholder="https://discord.com/api/webhooks/…"
                className={inputCls}
              />
            </Field>
          </div>
        </div>

        {/* Email card */}
        <div className="bg-surface-light border border-secondary/10 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-secondary/10">
            <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
              <Mail className="w-4 h-4 text-secondary" /> Email (SMTP)
            </h2>
            <button
              onClick={() => handleTest('email')}
              disabled={test.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-mono text-secondary bg-secondary/5 border border-secondary/20 rounded-lg hover:bg-secondary/15 disabled:opacity-50 transition-all"
            >
              <Send className="w-3 h-3" /> Test
            </button>
          </div>
          <div className="p-5 grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field label="SMTP host">
              <input
                value={form.email_smtp_host}
                onChange={(e) => set('email_smtp_host', e.target.value)}
                placeholder="smtp.example.com"
                className={inputCls}
              />
            </Field>
            <Field label="Port">
              <input
                value={form.email_smtp_port}
                onChange={(e) => set('email_smtp_port', e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="587"
                inputMode="numeric"
                className={inputCls}
              />
            </Field>
            <Field label="Username">
              <input
                value={form.email_smtp_user}
                onChange={(e) => set('email_smtp_user', e.target.value)}
                placeholder="user@example.com"
                className={inputCls}
              />
            </Field>
            <Field label="Password">
              <input
                type="password"
                value={form.email_smtp_password}
                onChange={(e) => set('email_smtp_password', e.target.value)}
                placeholder={settings.has_password ? '••• saved' : 'password'}
                className={inputCls}
              />
            </Field>
            <Field label="From">
              <input
                value={form.email_from}
                onChange={(e) => set('email_from', e.target.value)}
                placeholder="fade-out <noreply@example.com>"
                className={inputCls}
              />
            </Field>
            <Field label="To">
              <input
                value={form.email_to}
                onChange={(e) => set('email_to', e.target.value)}
                placeholder="you@example.com"
                className={inputCls}
              />
            </Field>
            <label className="flex items-center gap-2 sm:col-span-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={form.email_smtp_secure}
                onChange={(e) => set('email_smtp_secure', e.target.checked)}
                className="accent-[#7CB342]"
              />
              <span className="text-xs text-gray-400 font-mono">Use SSL/TLS</span>
            </label>
          </div>
        </div>
      </div>

      {/* Event toggles + min level */}
      <div className="bg-surface-light border border-primary/10 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-primary/10">
          <h2 className="text-sm font-mono text-gray-300 flex items-center gap-2">
            <SlidersHorizontal className="w-4 h-4 text-primary" /> Events
          </h2>
        </div>
        <div className="p-5 space-y-5">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {eventKeys.map((key) => (
              <label
                key={key}
                className="flex items-center gap-3 px-3 py-2.5 bg-white/[0.02] rounded-lg cursor-pointer select-none hover:bg-white/[0.04] transition-colors"
              >
                <input
                  type="checkbox"
                  checked={form.events[key] ?? false}
                  onChange={(e) => set('events', { ...form.events, [key]: e.target.checked })}
                  className="accent-[#7CB342]"
                />
                <div>
                  <p className="text-xs text-gray-300">{EVENT_LABELS[key] ?? key}</p>
                  <p className="text-[10px] text-gray-600 font-mono">{key}</p>
                </div>
              </label>
            ))}
          </div>
          <Field label="Minimum severity">
            <select
              value={form.min_level}
              onChange={(e) => set('min_level', e.target.value as FormState['min_level'])}
              className="bg-surface border border-white/10 rounded-lg px-3 py-2 text-sm font-mono text-gray-300 focus:outline-none focus:border-primary/40"
            >
              {MIN_LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </div>

      {/* Save */}
      <div className="flex justify-end">
        <button
          onClick={handleSave}
          disabled={!dirty || update.isPending}
          className="flex items-center gap-2 px-5 py-2.5 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 disabled:opacity-40 transition-all"
        >
          <Save className="w-4 h-4" />
          {update.isPending ? 'Saving…' : 'Save Settings'}
        </button>
      </div>
    </div>
  )
}

const tabs = [
  { key: 'settings', label: 'Settings', icon: SlidersHorizontal },
  { key: 'history', label: 'History', icon: History },
] as const

type TabKey = (typeof tabs)[number]['key']

export default function NotificationsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab: TabKey = searchParams.get('tab') === 'history' ? 'history' : 'settings'

  const setTab = (t: TabKey) =>
    setSearchParams(t === 'settings' ? {} : { tab: t }, { replace: true })

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
          <Bell className="w-6 h-6" /> Notifications
        </h1>
        <p className="text-sm text-gray-500 mt-1">Channels, event rules, and delivery history</p>
      </div>

      {/* Tabs */}
      <div className="border-b border-primary/10">
        <div className="flex gap-1 -mb-px">
          {tabs.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={clsx(
                'flex items-center gap-2 px-4 py-3 text-sm font-mono border-b-2 transition-all',
                tab === key
                  ? 'text-primary border-primary'
                  : 'text-gray-500 border-transparent hover:text-gray-300 hover:border-primary/20',
              )}
            >
              <Icon className="w-4 h-4" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'settings' ? <SettingsTab /> : <NotificationHistoryList />}
    </div>
  )
}
