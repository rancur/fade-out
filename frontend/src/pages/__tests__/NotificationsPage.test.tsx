import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import NotificationsPage from '../NotificationsPage'

vi.mock('@/api/ws', () => ({
  wsManager: {
    subscribe: () => () => {},
    subscribeStatus: () => () => {},
    connected: false,
    degraded: true,
  },
}))

vi.mock('@/api/client', () => ({
  default: {
    get: vi.fn((url: string) => {
      if (url === '/notifications/settings') {
        return Promise.resolve({
          data: {
            discord_webhook_url: 'https://discord.com/api/webhooks/123/abc',
            email_smtp_host: 'smtp.example.com',
            email_smtp_port: 587,
            email_smtp_user: 'will',
            has_password: true,
            email_from: 'fade-out@example.com',
            email_to: 'will@example.com',
            email_smtp_secure: true,
            webhook_urls: [],
            events: {
              pipeline_started: true,
              step_completed: false,
              upload_complete: true,
              error: true,
              draft_ready: true,
              upgrade_available: false,
            },
            min_level: 'info',
          },
        })
      }
      if (url === '/notifications') {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 1,
                mix_id: 'mix-abc',
                type: 'success',
                channel: 'discord',
                message: 'Upload complete: Friday Night Mix',
                sent: true,
                sent_at: new Date().toISOString(),
                created_at: new Date().toISOString(),
              },
            ],
            total: 1,
            page: 1,
            page_size: 50,
          },
        })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn(() => Promise.resolve({ data: { success: true, channel: 'discord', detail: 'ok' } })),
    put: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))

function renderPage(initialEntry = '/notifications') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <NotificationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('NotificationsPage', () => {
  it('renders the Settings tab with channel cards populated from the API', async () => {
    renderPage()

    // Channel cards
    expect(await screen.findByText('Discord')).toBeTruthy()
    expect(screen.getByText('Email (SMTP)')).toBeTruthy()

    // Values loaded into the form
    const webhookInput = screen.getByPlaceholderText(
      'https://discord.com/api/webhooks/…',
    ) as HTMLInputElement
    expect(webhookInput.value).toBe('https://discord.com/api/webhooks/123/abc')

    const hostInput = screen.getByPlaceholderText('smtp.example.com') as HTMLInputElement
    expect(hostInput.value).toBe('smtp.example.com')

    // Saved password shown as placeholder, never as a value
    const pwInput = screen.getByPlaceholderText('••• saved') as HTMLInputElement
    expect(pwInput.value).toBe('')

    // Event toggles
    expect(screen.getByText('Pipeline started')).toBeTruthy()
    expect(screen.getByText('Upload complete')).toBeTruthy()
    expect(screen.getByText('Draft ready for review')).toBeTruthy()

    // Save disabled while form is pristine
    const save = screen.getByRole('button', { name: /save settings/i }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
  })

  it('renders the History tab with delivery rows', async () => {
    renderPage('/notifications?tab=history')

    expect(await screen.findByText('Upload complete: Friday Night Mix')).toBeTruthy()
    // "discord" appears both as a channel filter chip and the row badge
    expect(screen.getAllByText('discord').length).toBeGreaterThan(1)
    expect(screen.getByText('sent')).toBeTruthy()

    const activityLink = screen.getByRole('link', { name: /delivery activity/i })
    expect(activityLink.getAttribute('href')).toBe('/activity?q=notification')
  })
})
