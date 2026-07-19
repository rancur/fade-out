import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ReviewQueue from '../ReviewQueue'

vi.mock('@/api/ws', () => ({
  wsManager: {
    subscribe: () => () => {},
    subscribeStatus: () => () => {},
    connected: false,
    degraded: true,
  },
}))

const proposals = [
  {
    id: 'p1',
    mix_id: 'mix-1',
    mix_title: 'Friday Night Warehouse Set',
    platform: 'youtube',
    field: 'title',
    current_value: 'untitled set 2024-03-01',
    proposed_value: 'Warehouse Techno All-Nighter | 2h Live Set',
    status: 'draft',
    created_by: 'ai',
    error: null,
    created_at: new Date().toISOString(),
    updated_at: null,
    applied_at: null,
  },
  {
    id: 'p2',
    mix_id: 'mix-1',
    mix_title: 'Friday Night Warehouse Set',
    platform: 'both',
    field: 'description',
    current_value: 'old line\nshared line',
    proposed_value: 'new line\nshared line',
    status: 'draft',
    created_by: 'user',
    error: null,
    created_at: new Date().toISOString(),
    updated_at: null,
    applied_at: null,
  },
]

vi.mock('@/api/client', () => ({
  default: {
    get: vi.fn((url: string) => {
      if (url === '/catalog/proposals') {
        return Promise.resolve({ data: { items: proposals, total: 2 } })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn((url: string) => {
      if (url === '/catalog/proposals/p1/approve') {
        return Promise.resolve({ data: { ...proposals[0], status: 'approved' } })
      }
      if (url === '/catalog/proposals/approve-bulk') {
        return Promise.resolve({ data: { approved: 2, applying: true } })
      }
      return Promise.resolve({ data: {} })
    }),
    put: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))

import client from '@/api/client'

function renderPage(entry = '/catalog/review') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <ReviewQueue />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ReviewQueue', () => {
  it('renders proposals grouped by mix with diffs and chips', async () => {
    renderPage()

    // Group header (mix title links to the editor)
    const mixLink = await screen.findByRole('link', { name: 'Friday Night Warehouse Set' })
    expect(mixLink.getAttribute('href')).toBe('/catalog/mix-1')

    // Title diff: before -> after
    expect(screen.getByText('untitled set 2024-03-01')).toBeTruthy()
    expect(screen.getByText('Warehouse Techno All-Nighter | 2h Live Set')).toBeTruthy()

    // Description line diff highlights added and removed lines
    expect(screen.getByText('old line')).toBeTruthy()
    expect(screen.getByText('new line')).toBeTruthy()

    // created_by chips
    expect(screen.getByText('ai')).toBeTruthy()
    expect(screen.getByText('user')).toBeTruthy()
  })

  it('approves a proposal via POST /catalog/proposals/{id}/approve', async () => {
    renderPage()

    const approveButtons = await screen.findAllByTitle('Approve')
    fireEvent.click(approveButtons[0])

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/catalog/proposals/p1/approve')
    })
  })

  it('bulk-approves selected proposals', async () => {
    renderPage()

    // Select both drafts, then bulk approve
    fireEvent.click(await screen.findByText('select all'))
    const bulkButton = await screen.findByRole('button', { name: /approve selected \(2\)/i })
    fireEvent.click(bulkButton)

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/catalog/proposals/approve-bulk', {
        ids: ['p1', 'p2'],
      })
    })
  })
})
