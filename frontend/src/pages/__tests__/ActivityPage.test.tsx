import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ActivityPage from '../ActivityPage'

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
      if (url === '/activity') {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 1,
                ts: new Date().toISOString(),
                level: 'info',
                event: 'pipeline_started',
                message: 'Pipeline started for Friday Night Mix',
                mix_id: 'mix-abc',
                filename: null,
                platform: 'soundcloud',
                stage: null,
                context: { step: 'analyze' },
              },
              {
                id: 2,
                ts: new Date().toISOString(),
                level: 'error',
                event: 'upload_failed',
                message: 'SoundCloud upload failed',
                mix_id: null,
                filename: null,
                platform: null,
                stage: null,
                context: null,
              },
            ],
            total: 2,
            limit: 50,
            offset: 0,
          },
        })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

function renderPage(initialEntry = '/activity') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <ActivityPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ActivityPage', () => {
  it('renders fetched activity rows with event badges and mix link', async () => {
    renderPage()

    expect(await screen.findByText('Pipeline started for Friday Night Mix')).toBeTruthy()
    expect(screen.getByText('SoundCloud upload failed')).toBeTruthy()
    // Event names appear both as row badges and in the event-type select
    expect(screen.getAllByText('pipeline_started').length).toBeGreaterThan(0)
    expect(screen.getAllByText('upload_failed').length).toBeGreaterThan(0)

    const mixLink = screen.getByRole('link', { name: /view mix/i })
    expect(mixLink.getAttribute('href')).toBe('/mixes/mix-abc')
  })

  it('renders the filter toolbar', async () => {
    renderPage()

    await screen.findByText('Pipeline started for Friday Night Mix')
    expect(screen.getByPlaceholderText('search messages…')).toBeTruthy()
    expect(screen.getByPlaceholderText('filter by mix id…')).toBeTruthy()
    // level chips
    for (const level of ['all', 'info', 'warn', 'error']) {
      expect(screen.getByRole('button', { name: level })).toBeTruthy()
    }
  })

  it('seeds the mix filter from ?mix_id= query param', async () => {
    renderPage('/activity?mix_id=mix-abc')

    await screen.findByText('Pipeline started for Friday Night Mix')
    const input = screen.getByPlaceholderText('filter by mix id…') as HTMLInputElement
    expect(input.value).toBe('mix-abc')
  })
})
