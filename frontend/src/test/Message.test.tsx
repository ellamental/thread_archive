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

  it('blocks remote markdown images but permits archived same-origin blobs', () => {
    const { container } = render(
      <Message
        message={msg([{
          type: 'text',
          text: '![tracker](https://tracker.example/pixel?id=secret) ![saved](/api/blob/abc123.png)',
        }])}
      />,
    )
    expect(screen.getByRole('note')).toHaveTextContent('image blocked · tracker')
    expect(container.querySelector('img[src^="https://"]')).toBeNull()
    expect(container.querySelector('img[src="/api/blob/abc123.png"]')).toHaveAttribute(
      'loading',
      'lazy',
    )
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

  it('links a pr_link block out to the forge', () => {
    render(
      <Message
        message={msg([
          {
            type: 'pr_link',
            ref: 'ellamental/thread_archive#4',
            repo: 'ellamental/thread_archive',
            number: '4',
            url: 'https://github.com/ellamental/thread_archive/pull/4',
          },
        ])}
      />,
    )
    const link = screen.getByRole('link', { name: 'ellamental/thread_archive#4' })
    expect(link).toHaveAttribute('href', 'https://github.com/ellamental/thread_archive/pull/4')
  })

  it('renders a pr_link with no url as plain text rather than a dead link', () => {
    render(
      <Message
        message={msg([
          { type: 'pr_link', ref: '#4', repo: null, number: '4', url: null },
        ])}
      />,
    )
    expect(screen.getByText('#4')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('never swallows an unrecognized block type', () => {
    const unknown = { type: 'holo_frame', text: 'future content' } as unknown as Block
    render(<Message message={msg([unknown])} />)
    expect(screen.getByText('holo_frame')).toBeInTheDocument()
    expect(screen.getByText('future content')).toBeInTheDocument()
  })

  it('renders a hook with its name and injected content', () => {
    render(
      <Message
        message={msg([
          { type: 'hook', hook_name: 'UserPromptSubmit', text: '[system-map] archive notes' },
        ])}
      />,
    )
    expect(screen.getByText('UserPromptSubmit')).toBeInTheDocument()
    expect(screen.getByText('hook')).toBeInTheDocument()
    expect(screen.getByText('[system-map] archive notes')).toBeInTheDocument()
  })

  it('renders an attachment with its type and real content', () => {
    render(
      <Message
        message={msg([
          { type: 'attachment', attachment_type: 'skill_listing', text: '- backend-api: …' },
        ])}
      />,
    )
    expect(screen.getByText(/attachment · skill_listing/)).toBeInTheDocument()
    expect(screen.getByText('- backend-api: …')).toBeInTheDocument()
  })

  it('renders block images inline with a click-through to the blob', () => {
    const { container } = render(
      <Message
        message={msg([
          {
            type: 'text',
            text: 'look at this',
            images: [
              {
                kind: 'image',
                media_type: 'image/png',
                bytes: 4096,
                url: '/api/blob/abc123.png',
              },
            ],
          },
        ])}
      />,
    )
    const img = container.querySelector('img.block-image')
    expect(img).not.toBeNull()
    expect(img?.getAttribute('src')).toBe('/api/blob/abc123.png')
    expect(img?.closest('a')?.getAttribute('href')).toBe('/api/blob/abc123.png')
  })

  it('renders an image-only user turn and labels unarchived pointers', () => {
    render(
      <Message
        message={msg(
          [
            {
              type: 'text',
              text: '',
              images: [
                { kind: 'image', media_type: 'image/png', bytes: null, url: null, pointer: 'file-svc://x' },
              ],
            },
          ],
          { role: 'user' },
        )}
      />,
    )
    expect(screen.getByText(/image · not archived/)).toBeInTheDocument()
  })

  it('shows tool-result images behind the result fold with a count', () => {
    render(
      <Message
        message={msg([
          {
            type: 'tool_result',
            output: '',
            truncated: false,
            images: [
              { kind: 'image', media_type: 'image/png', bytes: 2048, url: '/api/blob/def456.png' },
            ],
          },
        ])}
      />,
    )
    expect(screen.getByText(/result · 1 image/)).toBeInTheDocument()
  })

  it('merges consecutive hook firings into one chip row with counts', () => {
    const { container } = render(
      <Message
        message={msg([
          { type: 'hook_fired', hook_name: 'PreToolUse:Read' },
          { type: 'hook_fired', hook_name: 'PreToolUse:Read' },
          { type: 'hook_fired', hook_name: 'PostToolUse:Read' },
          { type: 'text', text: 'then some text' },
          { type: 'hook_fired', hook_name: 'PreToolUse:Edit' },
        ])}
      />,
    )
    // First run merges into a single row: deduped chips, repeat counted.
    const rows = container.querySelectorAll('.hook-fires')
    expect(rows).toHaveLength(2)
    expect(screen.getByText('PreToolUse:Read ×2')).toBeInTheDocument()
    expect(screen.getByText('PostToolUse:Read')).toBeInTheDocument()
    // The text block breaks the run; the later firing starts its own row.
    expect(screen.getByText('PreToolUse:Edit')).toBeInTheDocument()
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
