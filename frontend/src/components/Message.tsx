import { Markdown } from './Markdown'
import type { Block, Message as Msg } from '../api'

function prettyInput(input: unknown): string {
  if (typeof input === 'string') return input
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

function BlockView({ block }: { block: Block }) {
  switch (block.type) {
    case 'text':
      return <Markdown>{block.text}</Markdown>
    case 'thinking':
      return (
        <details className="tool thinking">
          <summary>thinking</summary>
          <Markdown>{block.text}</Markdown>
        </details>
      )
    case 'tool_use':
      return (
        <details className="tool">
          <summary>
            <span className="tool-name">{block.name}</span>
            <span className="tool-tag">tool call</span>
          </summary>
          <pre className="tool-body">{prettyInput(block.input)}</pre>
        </details>
      )
    case 'tool_result':
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">result{block.truncated ? ' · truncated' : ''}</span>
          </summary>
          <pre className="tool-body">{block.output}</pre>
        </details>
      )
    case 'tool_error':
      return (
        <details className="tool error" open>
          <summary>
            <span className="tool-tag">tool error</span>
          </summary>
          <pre className="tool-body">{block.error}</pre>
        </details>
      )
    case 'context_summary':
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">context summary</span>
          </summary>
          <Markdown>{block.text}</Markdown>
        </details>
      )
  }
}

export function Message({ message }: { message: Msg }) {
  return (
    <div className={'msg ' + message.role}>
      <div className="role">{message.role}</div>
      {message.blocks.map((b, i) => (
        <BlockView key={i} block={b} />
      ))}
    </div>
  )
}
