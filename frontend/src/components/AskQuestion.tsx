import { Markdown } from './Markdown'
import type { AskOption, AskQuestion } from '../api'

// AskUserQuestion is a decision point, not machinery: the assistant put a choice to
// the operator and the operator picked. Rendered as the JSON blob it arrives as,
// that reads as noise — so the reader renders the question, the options it weighed,
// and which one won, with the alternatives kept (they are the tradeoff space the
// decision was made against).

function asOption(raw: unknown): AskOption | null {
  if (typeof raw !== 'object' || raw === null) return null
  const o = raw as Record<string, unknown>
  if (typeof o.label !== 'string') return null
  return {
    label: o.label,
    description: typeof o.description === 'string' ? o.description : undefined,
    preview: typeof o.preview === 'string' ? o.preview : undefined,
  }
}

// Archive content is whatever the harness wrote years ago — read the shape
// defensively and fall back to the raw-JSON tool rendering when it doesn't hold.
export function askQuestions(input: unknown): AskQuestion[] | null {
  if (typeof input !== 'object' || input === null) return null
  const raw = (input as Record<string, unknown>).questions
  if (!Array.isArray(raw)) return null
  const questions: AskQuestion[] = []
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) return null
    const q = item as Record<string, unknown>
    if (typeof q.question !== 'string') return null
    const options = Array.isArray(q.options) ? q.options.map(asOption) : []
    if (options.some((o) => o === null)) return null
    questions.push({
      question: q.question,
      header: typeof q.header === 'string' ? q.header : undefined,
      multiSelect: q.multiSelect === true,
      options: options as AskOption[],
    })
  }
  return questions.length > 0 ? questions : null
}

// Resolve a recorded answer against the options it was offered. The answer is the
// chosen option's label — or, when the reader wrote their own instead of taking an
// option, their text; a multi-select answer is the labels joined by ", ". Anything
// that doesn't resolve to labels is treated as their own words rather than guessed at.
function resolveAnswer(
  answer: string | undefined,
  options: AskOption[],
): { chosen: Set<string>; wroteIn: string | null } {
  const none = { chosen: new Set<string>(), wroteIn: null }
  if (answer == null) return none
  const labels = new Set(options.map((o) => o.label))
  if (labels.has(answer)) return { chosen: new Set([answer]), wroteIn: null }
  const parts = answer.split(', ')
  if (parts.length > 1 && parts.every((p) => labels.has(p)))
    return { chosen: new Set(parts), wroteIn: null }
  return { chosen: new Set<string>(), wroteIn: answer }
}

function ChosenMark() {
  return (
    <span className="ask-mark" role="img" aria-label="chosen">
      ✓
    </span>
  )
}

function Option({ option, chosen }: { option: AskOption; chosen: boolean }) {
  return (
    <li className={'ask-option' + (chosen ? ' chosen' : '')}>
      <div className="ask-option-label">
        {chosen && <ChosenMark />}
        {/* markdown, like the description: labels routinely carry `code spans` and
            they read as literal backticks otherwise */}
        <Markdown>{option.label}</Markdown>
      </div>
      {option.description && (
        <div className="ask-option-desc">
          <Markdown>{option.description}</Markdown>
        </div>
      )}
      {option.preview && (
        // A .tool details so the reader's global collapse/expand reaches it; the
        // chosen option's preview opens on its own, the rejected ones stay folded.
        <details className="tool ask-preview" open={chosen}>
          <summary>
            <span className="tool-tag">preview</span>
          </summary>
          <pre className="tool-body">{option.preview}</pre>
        </details>
      )}
    </li>
  )
}

function Question({ question, answer }: { question: AskQuestion; answer?: string }) {
  const { chosen, wroteIn } = resolveAnswer(answer, question.options)
  return (
    <div className="ask-question">
      <div className="ask-question-head">
        {question.header && <span className="ask-header">{question.header}</span>}
        {question.multiSelect && <span className="ask-state">multi-select</span>}
      </div>
      <div className="ask-prompt">
        <Markdown>{question.question}</Markdown>
      </div>
      {question.options.length > 0 && (
        <ul className="ask-options">
          {question.options.map((o, i) => (
            <Option key={i} option={o} chosen={chosen.has(o.label)} />
          ))}
        </ul>
      )}
      {wroteIn !== null && (
        <div className="ask-wrote-in">
          <ChosenMark />
          <span className="ask-tag">own answer</span>
          <Markdown>{wroteIn}</Markdown>
        </div>
      )}
    </div>
  )
}

export function AskUserQuestionView({
  questions,
  answers,
  dismissed,
}: {
  questions: AskQuestion[]
  // Question text → the answer recorded for it. Absent when the question was never
  // answered (the turn was interrupted, or the reader turned the tool call down).
  answers?: Record<string, string>
  // The user rejected the tool call outright — asked, deliberately not answered.
  dismissed?: boolean
}) {
  const answered = answers != null && Object.keys(answers).length > 0
  return (
    <div className={'ask' + (answered ? ' answered' : '')}>
      <div className="ask-head">
        <span className="ask-tag">asked</span>
        {dismissed && <span className="ask-state">dismissed — no answer</span>}
        {!dismissed && !answered && <span className="ask-state">no answer recorded</span>}
      </div>
      {questions.map((q, i) => (
        <Question key={i} question={q} answer={answers?.[q.question]} />
      ))}
    </div>
  )
}
