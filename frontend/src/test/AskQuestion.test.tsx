// AskUserQuestion renders as the decision it is: the question, the options that
// were weighed, and the one the operator took — with the call and its answer folded
// into a single card rather than a JSON blob followed by a prose recap.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Message } from '../components/Message'
import type { Block, Message as Msg } from '../api'

const QUESTIONS = [
  {
    question: 'Which way should folding default?',
    header: 'Fold',
    multiSelect: false,
    options: [
      {
        label: 'fold=True default',
        description: "Today's behavior is unchanged for every existing caller.",
        preview: 'search(q)  -> folds',
      },
      { label: 'Stop folding by default', description: 'One less concept to carry.' },
    ],
  },
]

function ask(input: unknown = { questions: QUESTIONS }): Block {
  return { type: 'tool_use', name: 'AskUserQuestion', input }
}

function answered(answers: Record<string, string>): Block {
  return { type: 'tool_result', output: 'The user answered: …', truncated: false, answers }
}

function msg(blocks: Block[]): Msg {
  return { role: 'assistant', blocks }
}

function chosenOption(container: HTMLElement): string | undefined {
  return container.querySelector('.ask-option.chosen')?.textContent ?? undefined
}

describe('AskUserQuestion rendering', () => {
  it('renders the question and every option instead of the raw input JSON', () => {
    const { container } = render(<Message message={msg([ask()])} />)
    expect(screen.getByText('Which way should folding default?')).toBeInTheDocument()
    expect(screen.getByText('Fold')).toBeInTheDocument()
    expect(screen.getByText('fold=True default')).toBeInTheDocument()
    expect(screen.getByText('Stop folding by default')).toBeInTheDocument()
    expect(screen.getByText(/behavior is unchanged/)).toBeInTheDocument()
    expect(container.querySelector('.tool-name')).toBeNull()
  })

  it('marks the option the user took and drops the prose recap', () => {
    const { container } = render(
      <Message
        message={msg([
          ask(),
          answered({ 'Which way should folding default?': 'Stop folding by default' }),
        ])}
      />,
    )
    expect(chosenOption(container)).toContain('Stop folding by default')
    expect(container.querySelectorAll('.ask-option.chosen')).toHaveLength(1)
    expect(screen.getByLabelText('chosen')).toBeInTheDocument()
    expect(screen.queryByText(/The user answered/)).toBeNull()
  })

  it('opens the chosen option’s preview and leaves the rejected ones folded', () => {
    const { container } = render(
      <Message
        message={msg([ask(), answered({ 'Which way should folding default?': 'fold=True default' })])}
      />,
    )
    const preview = container.querySelector('.ask-option.chosen details.ask-preview')
    expect(preview).toHaveAttribute('open')
    // verbatim, spacing intact — a preview is a mockup, not prose
    expect(preview?.querySelector('pre')?.textContent).toBe('search(q)  -> folds')
    expect(container.querySelector('.ask-option:not(.chosen) details.ask-preview')).toBeNull()
  })

  it('shows an answer the user typed themselves as their own words, not an option', () => {
    const { container } = render(
      <Message
        message={msg([
          ask(),
          answered({ 'Which way should folding default?': 'neither — leave browse alone' }),
        ])}
      />,
    )
    expect(container.querySelector('.ask-option.chosen')).toBeNull()
    expect(screen.getByText('neither — leave browse alone')).toBeInTheDocument()
    expect(screen.getByText('own answer')).toBeInTheDocument()
  })

  it('marks every pick of a multi-select answer', () => {
    const multi = [{ ...QUESTIONS[0], multiSelect: true }]
    const { container } = render(
      <Message
        message={msg([
          ask({ questions: multi }),
          answered({
            'Which way should folding default?': 'fold=True default, Stop folding by default',
          }),
        ])}
      />,
    )
    expect(container.querySelectorAll('.ask-option.chosen')).toHaveLength(2)
    expect(screen.getByText('multi-select')).toBeInTheDocument()
  })

  it('reads a rejected question as dismissed, without the harness refusal text', () => {
    render(
      <Message
        message={msg([
          ask(),
          {
            type: 'tool_error',
            error: 'The user doesn’t want to proceed with this tool use.',
            denial_kind: 'user-rejected',
          },
        ])}
      />,
    )
    expect(screen.getByText(/dismissed/)).toBeInTheDocument()
    expect(screen.queryByText(/doesn’t want to proceed/)).toBeNull()
  })

  it('keeps a genuine tool error visible next to the question it followed', () => {
    render(
      <Message
        message={msg([ask(), { type: 'tool_error', error: 'the harness crashed mid-question' }])}
      />,
    )
    expect(screen.getByText('the harness crashed mid-question')).toBeInTheDocument()
    expect(screen.getByText(/no answer recorded/)).toBeInTheDocument()
  })

  it('pairs the answer across the hook markers that fire around the call', () => {
    const { container } = render(
      <Message
        message={msg([
          ask(),
          { type: 'hook_fired', hook_name: 'PostToolUse:AskUserQuestion' },
          answered({ 'Which way should folding default?': 'fold=True default' }),
        ])}
      />,
    )
    expect(chosenOption(container)).toContain('fold=True default')
    expect(screen.getByText('PostToolUse:AskUserQuestion')).toBeInTheDocument()
  })

  it('renders an answer whose call block landed in another message', () => {
    const { container } = render(
      <Message
        message={msg([
          {
            type: 'tool_result',
            output: 'The user answered: …',
            truncated: false,
            questions: QUESTIONS,
            answers: { 'Which way should folding default?': 'Stop folding by default' },
          },
        ])}
      />,
    )
    expect(chosenOption(container)).toContain('Stop folding by default')
  })

  it('falls back to the raw tool rendering when the input is not question-shaped', () => {
    render(<Message message={msg([ask({ prompt: 'not the schema' })])} />)
    expect(screen.getByText('AskUserQuestion')).toBeInTheDocument()
    expect(screen.getByText('tool call')).toBeInTheDocument()
    expect(screen.getByText(/"prompt": "not the schema"/)).toBeInTheDocument()
  })

  it('falls back rather than half-render a question whose options are malformed', () => {
    const bad = { questions: [{ question: 'which?', options: [{ description: 'no label' }] }] }
    const { container } = render(<Message message={msg([ask(bad)])} />)
    expect(container.querySelector('.ask')).toBeNull()
    expect(screen.getByText(/"description": "no label"/)).toBeInTheDocument()
  })

  it('renders a question with no options at all', () => {
    render(<Message message={msg([ask({ questions: [{ question: 'open-ended?' }] })])} />)
    expect(screen.getByText('open-ended?')).toBeInTheDocument()
  })
})
