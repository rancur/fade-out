import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CatalogPage from '../CatalogPage'

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
      if (url === '/catalog/mixes') {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 'mix-1',
                title: 'Friday Night Warehouse Set',
                source: 'imported',
                platforms: ['youtube', 'soundcloud'],
                youtube_video_id: 'yt123',
                soundcloud_track_id: 'sc456',
                youtube_url: 'https://youtube.com/watch?v=yt123',
                soundcloud_url: 'https://soundcloud.com/x/y',
                thumbnail_url: null,
                artwork_url: null,
                duration_seconds: 3723,
                youtube_published_at: '2025-06-01T00:00:00Z',
                soundcloud_published_at: null,
                title_locked: false,
                open_proposals: 2,
              },
              {
                id: 'mix-2',
                title: 'Deep House Session 04',
                source: 'pipeline',
                platforms: ['soundcloud'],
                youtube_video_id: null,
                soundcloud_track_id: 'sc789',
                youtube_url: null,
                soundcloud_url: 'https://soundcloud.com/x/z',
                thumbnail_url: null,
                artwork_url: null,
                duration_seconds: 1800,
                youtube_published_at: null,
                soundcloud_published_at: '2025-07-01T00:00:00Z',
                title_locked: true,
                open_proposals: 0,
              },
            ],
            total: 2,
            page: 1,
            page_size: 12,
          },
        })
      }
      if (url === '/catalog/sync/status') {
        return Promise.resolve({ data: { running: false, last_sync: null } })
      }
      if (url === '/catalog/backfill-status') {
        return Promise.resolve({ data: { running: false, last_backfill: null } })
      }
      if (url === '/catalog/proposals') {
        return Promise.resolve({ data: { items: [], total: 5 } })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn(() => Promise.resolve({ data: { status: 'started' } })),
    put: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))

import client from '@/api/client'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/catalog']}>
        <CatalogPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CatalogPage', () => {
  it('renders catalog mixes with source chips and open-proposal badges', async () => {
    renderPage()

    expect(await screen.findByText('Friday Night Warehouse Set')).toBeTruthy()
    expect(screen.getByText('Deep House Session 04')).toBeTruthy()

    // Source chips (also appear once each as <option>s in the source select)
    expect(screen.getAllByText('imported').length).toBeGreaterThan(1)
    expect(screen.getAllByText('pipeline').length).toBeGreaterThan(1)

    // Open-proposal badge on mix-1 links to the filtered review queue
    const badge = screen.getByTitle('Open proposals — review them')
    expect(badge.getAttribute('href')).toBe('/catalog/review?mix_id=mix-1')
    expect(badge.textContent).toContain('2')

    // Review queue button shows draft count from the proposals endpoint
    const queueLink = screen.getByRole('link', { name: /review queue/i })
    expect(queueLink.textContent).toContain('5')
  })

  it('starts a sync via POST /catalog/sync', async () => {
    renderPage()

    const syncButton = await screen.findByRole('button', { name: /sync/i })
    fireEvent.click(syncButton)

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/catalog/sync')
    })
  })

  it('starts a tracklist backfill via POST /catalog/backfill-tracklists', async () => {
    renderPage()

    const backfillButton = await screen.findByRole('button', {
      name: /backfill tracklists/i,
    })
    fireEvent.click(backfillButton)

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/catalog/backfill-tracklists', {
        mix_ids: null,
      })
    })
  })

  it('shows the last backfill summary line when idle', async () => {
    const defaultGet = vi.mocked(client.get).getMockImplementation()!
    vi.mocked(client.get).mockImplementation((url: string) => {
      if (url === '/catalog/backfill-status') {
        return Promise.resolve({
          data: {
            running: false,
            last_backfill: {
              started_at: '2026-07-01T00:00:00Z',
              finished_at: '2026-07-01T02:00:00Z',
              status: 'ok',
              errors: [],
              matched: 4,
              processed: 4,
              tracks_found: 80,
              proposals_created: 8,
              unmatched_mixes: [{ mix_id: 'x', title: 'No Audio Anywhere' }],
            },
          },
        })
      }
      return defaultGet(url)
    })

    renderPage()

    expect(
      await screen.findByText(
        'Backfill ok: 4/4 matched mixes analyzed, 80 tracks found, 8 proposals approved, 1 mixes without local audio',
      ),
    ).toBeTruthy()
  })
})
