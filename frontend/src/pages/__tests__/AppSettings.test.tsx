import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AppSettings from '../AppSettings'

vi.mock('@/api/ws', () => ({
  wsManager: {
    subscribe: () => () => {},
    subscribeStatus: () => () => {},
    connected: false,
    degraded: true,
  },
}))

// Keep the OAuth wizard out of scope — it has its own flows and queries.
vi.mock('@/components/OAuthConnect', () => ({
  default: () => <div data-testid="setup-wizard" />,
}))

const schema = {
  categories: ['Paths', 'Pipeline', 'AI', 'YouTube', 'SoundCloud', 'Activity', 'Advanced'],
  settings: [
    {
      key: 'watch_audio_path',
      label: 'Audio watch folder',
      help: 'Folder watched for new audio drops. Bound to a container mount at deploy time.',
      type: 'path',
      category: 'Paths',
      value: '/watch/audio',
      has_value: true,
      default: '/watch/audio',
      editable: false,
      source: 'env',
      choices: null,
      min: null,
      max: null,
    },
    {
      key: 'draft_mode',
      label: 'Draft mode',
      help: 'Pause every pipeline for manual review.',
      type: 'bool',
      category: 'Pipeline',
      value: true,
      has_value: true,
      default: true,
      editable: true,
      source: 'db',
      choices: null,
      min: null,
      max: null,
    },
    {
      key: 'premiere_mode',
      label: 'Premiere mode',
      help: 'How YouTube uploads go live.',
      type: 'enum',
      category: 'Pipeline',
      value: 'scheduled',
      has_value: true,
      default: 'scheduled',
      editable: true,
      source: 'db',
      choices: ['instant', 'scheduled', 'unlisted'],
      min: null,
      max: null,
    },
    {
      key: 'youtube_daily_quota_budget',
      label: 'Daily quota budget (units)',
      help: 'YouTube Data API quota reserved per day.',
      type: 'int',
      category: 'YouTube',
      value: 8000,
      has_value: true,
      default: 8000,
      editable: true,
      source: 'env',
      choices: null,
      min: 0,
      max: 10000,
    },
    {
      key: 'openai_api_key',
      label: 'OpenAI API key',
      help: 'Write-only: the saved value is never shown.',
      type: 'secret',
      category: 'AI',
      value: null,
      has_value: true,
      default: null,
      editable: true,
      source: 'db',
      choices: null,
      min: null,
      max: null,
    },
  ],
}

const putMock = vi.fn(() => Promise.resolve({ data: schema }))

vi.mock('@/api/client', () => ({
  default: {
    get: vi.fn((url: string) => {
      if (url === '/settings/schema') return Promise.resolve({ data: schema })
      return Promise.resolve({ data: {} })
    }),
    put: vi.fn((...args: unknown[]) => putMock(...(args as []))),
    post: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <AppSettings />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  putMock.mockClear()
})

describe('AppSettings', () => {
  it('renders category tabs and the first category', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Paths')).toBeTruthy())
    expect(screen.getByText('Pipeline')).toBeTruthy()
    expect(screen.getByText('Advanced')).toBeTruthy()
    // First tab (Paths) content is shown by default
    expect(screen.getByText('Audio watch folder')).toBeTruthy()
    expect(screen.getByText('/watch/audio')).toBeTruthy()
    expect(screen.getByText('configured at deploy')).toBeTruthy()
    // Existing OAuth flow stays mounted
    expect(screen.getByTestId('setup-wizard')).toBeTruthy()
  })

  it('shows a masked write-only placeholder for saved secrets', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('AI')).toBeTruthy())
    fireEvent.click(screen.getByText('AI'))
    const secretInput = screen.getByPlaceholderText('•••• saved (write-only)')
    expect(secretInput.getAttribute('type')).toBe('password')
    expect((secretInput as HTMLInputElement).value).toBe('')
  })

  it('tracks dirty edits and saves only changed values', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Pipeline')).toBeTruthy())
    fireEvent.click(screen.getByText('Pipeline'))

    // Toggle draft mode off
    const toggle = screen.getByRole('button', { pressed: true })
    fireEvent.click(toggle)

    // Save bar appears with dirty count
    expect(screen.getByText('1 unsaved change')).toBeTruthy()

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1))
    expect(putMock).toHaveBeenCalledWith('/settings/values', {
      values: { draft_mode: false },
    })
  })

  it('blocks saving when a number is out of range', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('YouTube')).toBeTruthy())
    fireEvent.click(screen.getByText('YouTube'))

    const input = screen.getByRole('spinbutton')
    fireEvent.change(input, { target: { value: '99999' } })

    expect(screen.getByText('Must be at most 10000')).toBeTruthy()
    expect(screen.getByText('fix invalid values to save')).toBeTruthy()
    const save = screen.getByText('Save').closest('button')
    expect(save!.disabled).toBe(true)
    fireEvent.click(save!)
    expect(putMock).not.toHaveBeenCalled()
  })

  it('discards staged edits', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Pipeline')).toBeTruthy())
    fireEvent.click(screen.getByText('Pipeline'))
    fireEvent.click(screen.getByRole('button', { pressed: true }))
    expect(screen.getByText('1 unsaved change')).toBeTruthy()

    fireEvent.click(screen.getByText('Discard'))
    expect(screen.queryByText('1 unsaved change')).toBeNull()
  })
})
