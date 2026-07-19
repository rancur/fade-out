import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MixList from '../MixList'

vi.mock('@/api/ws', () => ({
  wsManager: {
    subscribe: () => () => {},
    subscribeStatus: () => () => {},
    connected: false,
    degraded: true,
  },
}))

vi.mock('@/components/MixCard', () => ({
  default: ({ mix }: { mix: { title: string } }) => <div>{mix.title}</div>,
}))

vi.mock('@/api/client', () => ({
  default: {
    get: vi.fn((url: string) => {
      if (url === '/mixes') {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 'mix-1',
                title: 'Warehouse Rituals',
                pipeline_status: 'uploaded',
                created_at: '2025-06-01T00:00:00Z',
              },
            ],
            total: 1,
            page: 1,
            page_size: 12,
          },
        })
      }
      return Promise.resolve({ data: {} })
    }),
    post: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))

import client from '@/api/client'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/mixes']}>
        <MixList />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('MixList', () => {
  it('fetches with sort=newest by default and refetches when sort changes', async () => {
    renderPage()

    expect(await screen.findByText('Warehouse Rituals')).toBeTruthy()
    expect(client.get).toHaveBeenCalledWith('/mixes', {
      params: expect.objectContaining({ sort: 'newest', page: 1 }),
    })

    fireEvent.change(screen.getByLabelText('Sort mixes'), {
      target: { value: 'title' },
    })

    await waitFor(() => {
      expect(client.get).toHaveBeenCalledWith('/mixes', {
        params: expect.objectContaining({ sort: 'title', page: 1 }),
      })
    })
  })
})
