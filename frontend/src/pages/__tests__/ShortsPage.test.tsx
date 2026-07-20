import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ShortsPage from '../ShortsPage'

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
      if (url === '/shorts') {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 'short-1',
                file_path: '/watch/shorts/Backtrack 2026-05-14 21-03-22.mp4',
                filename: 'Backtrack 2026-05-14 21-03-22.mp4',
                file_hash: 'h1',
                title: 'Four decks, one drop 🔥',
                description: 'Live moment\n#shorts #dj #house',
                tags: ['dj', 'house'],
                track_artist: 'Artist X',
                track_title: 'Track Y',
                duration_seconds: 42,
                width: 1080,
                height: 1920,
                youtube_video_id: null,
                youtube_url: null,
                status: 'queued',
                error: null,
                detected_at: '2026-05-14T21:05:00Z',
                uploaded_at: null,
                metadata_json: null,
              },
              {
                id: 'short-2',
                file_path: '/watch/shorts/Backtrack 2026-05-15 20-00-00.mp4',
                filename: 'Backtrack 2026-05-15 20-00-00.mp4',
                file_hash: 'h2',
                title: 'Desert warehouse energy 🌵',
                description: 'desc\n#shorts',
                tags: [],
                track_artist: null,
                track_title: null,
                duration_seconds: 58,
                width: 1080,
                height: 1920,
                youtube_video_id: 'ytdone',
                youtube_url: 'https://www.youtube.com/shorts/ytdone',
                status: 'uploaded',
                error: null,
                detected_at: '2026-05-15T20:05:00Z',
                uploaded_at: '2026-05-16T00:00:00Z',
                metadata_json: null,
              },
            ],
            total: 2,
            page: 1,
            page_size: 12,
          },
        })
      }
      if (url === '/shorts/stats') {
        return Promise.resolve({
          data: {
            uploaded_today: 3,
            daily_cap: 3,
            cap_reached: true,
            queued: 5,
            quota_used: 4800,
            quota_budget: 8000,
            status_counts: { queued: 5, uploaded: 24 },
            watcher_active: true,
            watch_path: '/watch/shorts',
          },
        })
      }
      if (url === '/shorts/scan/status') {
        return Promise.resolve({ data: { running: false, last_scan: null } })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn(() => Promise.resolve({ data: { status: 'started' } })),
    put: vi.fn(() =>
      Promise.resolve({ data: { id: 'short-1', title: 'Edited', description: 'd' } }),
    ),
  },
}))

import client from '@/api/client'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/shorts']}>
        <ShortsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ShortsPage', () => {
  it('renders shorts with track IDs, status chips, and the daily-cap indicator', async () => {
    renderPage()

    expect(await screen.findByText('Four decks, one drop 🔥')).toBeTruthy()
    expect(screen.getByText('Desert warehouse energy 🌵')).toBeTruthy()

    // Track credit + missing track ID state
    expect(screen.getByText('Artist X — Track Y')).toBeTruthy()
    expect(screen.getByText('no track ID')).toBeTruthy()

    // Daily-cap indicator from /shorts/stats
    expect(screen.getByText('3/3 uploaded today')).toBeTruthy()
    expect(screen.getByText('5 queued')).toBeTruthy()
    expect(screen.getByText('quota 4800/8000 units')).toBeTruthy()

    // Status chips (also appear as filter buttons — assert at least one chip)
    expect(screen.getAllByText('queued').length).toBeGreaterThan(0)
    expect(screen.getAllByText('uploaded').length).toBeGreaterThan(0)

    // Uploaded short links out to YouTube
    const watch = screen.getByRole('link', { name: /watch/i })
    expect(watch.getAttribute('href')).toBe('https://www.youtube.com/shorts/ytdone')
  })

  it('starts a backlog scan via POST /shorts/scan', async () => {
    renderPage()

    const scanButton = await screen.findByRole('button', { name: /scan folder/i })
    fireEvent.click(scanButton)

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/shorts/scan')
    })
  })

  it('uploads a queued short via POST /shorts/{id}/upload', async () => {
    renderPage()

    await screen.findByText('Four decks, one drop 🔥')
    // Only the queued short exposes "Upload now"
    const uploadButton = screen.getByRole('button', { name: /upload now/i })
    fireEvent.click(uploadButton)

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/shorts/short-1/upload')
    })
  })

  it('edits title inline and saves via PUT /shorts/{id}', async () => {
    renderPage()

    await screen.findByText('Four decks, one drop 🔥')
    fireEvent.click(screen.getAllByRole('button', { name: /edit/i })[0])

    const titleInput = screen.getByLabelText('Short title')
    fireEvent.change(titleInput, { target: { value: 'Edited title' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => {
      expect(client.put).toHaveBeenCalledWith('/shorts/short-1', {
        title: 'Edited title',
        description: 'Live moment\n#shorts #dj #house',
      })
    })
  })

  it('skips a short via POST /shorts/{id}/skip', async () => {
    renderPage()

    await screen.findByText('Four decks, one drop 🔥')
    fireEvent.click(screen.getAllByRole('button', { name: /^skip$/i })[0])

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/shorts/short-1/skip')
    })
  })

  it('regenerates metadata via POST /shorts/{id}/regenerate-metadata', async () => {
    renderPage()

    await screen.findByText('Four decks, one drop 🔥')
    fireEvent.click(screen.getAllByRole('button', { name: /regenerate/i })[0])

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/shorts/short-1/regenerate-metadata')
    })
  })

  it('requests a status-filtered list when a filter chip is clicked', async () => {
    renderPage()

    await screen.findByText('Four decks, one drop 🔥')
    fireEvent.click(screen.getByRole('button', { name: /failed/i }))

    await waitFor(() => {
      expect(client.get).toHaveBeenCalledWith('/shorts', {
        params: expect.objectContaining({ status: 'failed', page: 1 }),
      })
    })
  })
})
