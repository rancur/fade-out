import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CatalogMixEditor from '../CatalogMixEditor'

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
      if (url === '/mixes/mix-1') {
        return Promise.resolve({
          data: {
            id: 'mix-1',
            title: 'Friday Night Warehouse Set',
            source: 'imported',
            title_locked: false,
            title_youtube: 'Friday Night Warehouse Set (YT)',
            description_youtube: 'yt description',
            description_soundcloud: 'sc description',
            tags: ['techno', 'live'],
            youtube_video_id: 'yt123',
            soundcloud_track_id: 'sc456',
            youtube_url: 'https://youtube.com/watch?v=yt123',
            soundcloud_url: 'https://soundcloud.com/x/y',
            metadata_json: { catalog: { youtube: { thumbnail_url: 'https://img/thumb.jpg' } } },
            steps: [],
          },
        })
      }
      if (url === '/catalog/proposals') {
        return Promise.resolve({ data: { items: [], total: 0 } })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(() => Promise.resolve({ data: { proposals: [{ id: 'p1' }], applying: false } })),
  },
}))

import client from '@/api/client'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/catalog/mix-1']}>
        <Routes>
          <Route path="/catalog/:id" element={<CatalogMixEditor />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CatalogMixEditor', () => {
  it('keeps Save disabled while pristine and sends only dirty per-platform fields', async () => {
    renderPage()

    // Both platform panels render with current values
    const titleInputs = (await screen.findAllByLabelText('Title')) as HTMLInputElement[]
    expect(titleInputs).toHaveLength(2)
    expect(titleInputs[0].value).toBe('Friday Night Warehouse Set (YT)') // YouTube panel first
    expect(titleInputs[1].value).toBe('Friday Night Warehouse Set')

    const save = screen.getByRole('button', { name: /^save$/i }) as HTMLButtonElement
    expect(save.disabled).toBe(true)

    // Edit only the YouTube title
    fireEvent.change(titleInputs[0], { target: { value: 'Warehouse All-Nighter' } })
    expect(save.disabled).toBe(false)
    fireEvent.click(save)

    // PUT payload contains only the dirty youtube.title — no soundcloud key
    await waitFor(() => {
      expect(client.put).toHaveBeenCalledWith('/catalog/mixes/mix-1', {
        apply: false,
        youtube: { title: 'Warehouse All-Nighter' },
      })
    })
  })

  it('sets apply: true for Save & Apply', async () => {
    renderPage()

    const descInputs = (await screen.findAllByLabelText('Description')) as HTMLTextAreaElement[]
    fireEvent.change(descInputs[1], { target: { value: 'fresh sc description' } })

    fireEvent.click(screen.getByRole('button', { name: /save & apply/i }))

    await waitFor(() => {
      expect(client.put).toHaveBeenCalledWith('/catalog/mixes/mix-1', {
        apply: true,
        soundcloud: { description: 'fresh sc description' },
      })
    })
  })
})
