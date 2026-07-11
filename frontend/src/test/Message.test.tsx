// The message renderer: every typed block renders visibly (never silently
// swallowed), the info drawer folds out per-message metadata, and continuation
// messages merge into the previous panel without a fresh role label.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Message } from '../components/Message'
import type { Block, Message as Msg } from '../api'

function msg(blocks: Block[], extra: Partial<Msg> = {}): Msg {
  return { role: 'assistant', blocks, ...extra }
}

describe('block rendering', () => {
  it('renders text blocks as markdown', () => {
    render(<Message message={msg([{ type: 'text', text: 'plain **bold** words' }])} />)
    expect(screen.getByText('bold')).toBeInTheDocument()
    expect(screen.getByText('bold').tagName).toBe('STRONG')
  })

  it('folds thinking into a labelled details element', () => {
    render(<Message message={msg([{ type: 'thinking', text: 'internal reasoning' }])} />)
    expect(screen.getByText('thinking')).toBeInTheDocument()
    expect(screen.getByText('internal reasoning')).toBeInTheDocument()
  })

  it('shows tool calls with their tool name and pretty-printed input', () => {
    render(
      <Message
        message={msg([{ type: 'tool_use', name: 'Bash', input: { command: 'ls -la' } }])}
      />,
    )
    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('tool call')).toBeInTheDocument()
    expect(screen.getByText(/"command": "ls -la"/)).toBeInTheDocument()
  })

  it('marks truncated tool results', () => {
    render(
      <Message message={msg([{ type: 'tool_result', output: 'partial out', truncated: true }])} />,
    )
    expect(screen.getByText(/result · truncated/)).toBeInTheDocument()
    expect(screen.getByText('partial out')).toBeInTheDocument()
  })

  it('renders tool errors expanded', () => {
    const { container } = render(
      <Message message={msg([{ type: 'tool_error', error: 'exit code 1' }])} />,
    )
    expect(screen.getByText('exit code 1')).toBeVisible()
    expect(container.querySelector('details.error')).toHaveAttribute('open')
  })

  it('renders ide context with its file path', () => {
    render(
      <Message
        message={msg([
          { type: 'ide_context', context_type: 'selection', file_path: '/a/b.py', text: 'def f()' },
        ])}
      />,
    )
    expect(screen.getByText(/ide selection · \/a\/b\.py/)).toBeInTheDocument()
  })

  it('renders a user /model switch as a bare divider without a role label', () => {
    render(
      <Message
        message={{
          role: 'model_switch',
          blocks: [{ type: 'model_switch', kind: 'user', from_model: null, to_model: 'opus' }],
        }}
      />,
    )
    expect(screen.getByText('opus')).toBeInTheDocument()
    expect(screen.getByText(/switched to/)).toBeInTheDocument()
    expect(screen.queryByText('model_switch')).not.toBeInTheDocument()
  })

  it('shows both models for a safeguard fallback switch', () => {
    render(
      <Message
        message={msg([
          { type: 'model_switch', kind: 'fallback', from_model: 'haiku', to_model: 'opus' },
        ])}
      />,
    )
    expect(screen.getByText(/model switch ·/)).toBeInTheDocument()
    expect(screen.getByText('haiku')).toBeInTheDocument()
    expect(screen.getByText('opus')).toBeInTheDocument()
  })

  it('never swallows an unrecognized block type', () => {
    const unknown = { type: 'holo_frame', text: 'future content' } as unknown as Block
    render(<Message message={msg([unknown])} />)
    expect(screen.getByText('holo_frame')).toBeInTheDocument()
    expect(screen.getByText('future content')).toBeInTheDocument()
  })
})

describe('info drawer', () => {
  it('opens on ⓘ and shows model, compact token counts, and timestamp', async () => {
    const user = userEvent.setup()
    render(
      <Message
        message={msg([{ type: 'text', text: 'hi' }], {
          meta: {
            ts: '2026-01-01T10:00:00Z',
            models: ['claude-opus-4'],
            requests: 2,
            stop_reason: 'end_turn',
            tokens: { input: 12600, output: 512, thinking: 0 },
          },
        })}
      />,
    )
    expect(screen.queryByText('claude-opus-4')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'message info' }))
    expect(screen.getByText('claude-opus-4')).toBeInTheDocument()
    expect(screen.getByText(/12\.6k ctx · 512 out/)).toBeInTheDocument()
    expect(screen.getByText('2 req')).toBeInTheDocument()
    expect(screen.getByText('end_turn')).toBeInTheDocument()
  })

  it('says so when a message has no model metadata', async () => {
    const user = userEvent.setup()
    render(<Message message={msg([{ type: 'text', text: 'hi' }], { role: 'user' })} />)
    await user.click(screen.getByRole('button', { name: 'message info' }))
    expect(screen.getByText('no model metadata')).toBeInTheDocument()
  })

  it('toggles raw view from the drawer', async () => {
    const user = userEvent.setup()
    render(<Message message={msg([{ type: 'text', text: 'hi' }])} />)
    await user.click(screen.getByRole('button', { name: 'message info' }))
    await user.click(screen.getByText('view as raw'))
    expect(screen.getByText('✓ viewing raw')).toBeInTheDocument()
  })
})

describe('continuation grouping', () => {
  it('labels a fresh message with its role but not a continuation', () => {
    const { rerender } = render(<Message message={msg([{ type: 'text', text: 'one' }])} />)
    expect(screen.getByText('assistant')).toBeInTheDocument()
    rerender(<Message message={msg([{ type: 'text', text: 'one' }])} continued />)
    expect(screen.queryByText('assistant')).not.toBeInTheDocument()
  })
})
