import { useState } from 'react'
import { Markdown } from './Markdown'
import { RawContext } from './RawMode'
import { hueStyle, modelHue } from '../modelColor'
import type { Block, Message as Msg, MessageMeta } from '../api'

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
    case 'ide_context':
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">
              ide {block.context_type}{block.file_path ? ` · ${block.file_path}` : ''}
            </span>
          </summary>
          <pre className="tool-body">{block.text}</pre>
        </details>
      )
    case 'content_block':
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">block · {block.block_type}</span>
          </summary>
          <pre className="tool-body">{block.text}</pre>
        </details>
      )
    default:
      // Never silently swallow a block type we don't recognize — show it.
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">
              {(block as { event_type?: string }).event_type ||
                (block as { type?: string }).type ||
                'unknown'}
            </span>
          </summary>
          <pre className="tool-body">
            {(block as { text?: string }).text ?? JSON.stringify(block, null, 2)}
          </pre>
        </details>
      )
  }
}

// Compact token count ("12.6k"), matching cloth's turn-meta formatting.
function fmt(n?: number): string {
  if (!n) return '0'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function fmtTime(s: string | null | undefined): string | null {
  if (!s) return null
  const d = new Date(s)
  return isNaN(d.getTime()) ? s : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

// The drawer that expands under the message: model/request detail (folded from the
// turn's api_request events) plus a "view as raw" toggle. Modeled on cloth's inline
// turn-meta — under the message, not a side sheet.
function MessageMeta({
  meta,
  raw,
  onToggleRaw,
  hueForModel,
}: {
  meta?: MessageMeta
  raw: boolean
  onToggleRaw: (v: boolean) => void
  hueForModel?: Record<string, number>
}) {
  const models = meta?.models ?? []
  const tok = meta?.tokens
  const time = fmtTime(meta?.ts)
  return (
    <div className="msg-meta">
      <div className="msg-meta-row">
        {models.map((m) => (
          <span className="chip model-chip" key={m} style={hueStyle(hueForModel?.[m] ?? modelHue(m))}>
            {m}
          </span>
        ))}
        {tok && tok.input + tok.output + tok.thinking > 0 && (
          <span>
            {fmt(tok.input)} ctx · {fmt(tok.output)} out
            {tok.thinking > 0 && ` · ${fmt(tok.thinking)} thinking`}
          </span>
        )}
        {meta?.requests != null && meta.requests > 0 && <span>{meta.requests} req</span>}
        {meta?.stop_reason && <span>{meta.stop_reason}</span>}
        {time && <span>{time}</span>}
        {models.length === 0 && !tok && <span>no model metadata</span>}
      </div>
      <button className={'raw-toggle' + (raw ? ' on' : '')} onClick={() => onToggleRaw(!raw)}>
        {raw ? '✓ viewing raw' : 'view as raw'}
      </button>
    </div>
  )
}

export function Message({
  message,
  hueForModel,
}: {
  message: Msg
  hueForModel?: Record<string, number>
}) {
  const [open, setOpen] = useState(false)
  const [raw, setRaw] = useState(false)
  // Tint an assistant message to match the model that produced it (its turn's first
  // model — near-always its only one). Other roles carry no model, so no accent.
  const model = message.role === 'assistant' ? message.meta?.models?.[0] : undefined
  const hue = model ? (hueForModel?.[model] ?? modelHue(model)) : undefined
  return (
    <div
      className={'msg ' + message.role + (hue != null ? ' has-model' : '')}
      style={hue != null ? hueStyle(hue) : undefined}
    >
      <div className="role">{message.role}</div>
      <RawContext.Provider value={raw}>
        {message.blocks.map((b, i) => (
          <BlockView key={i} block={b} />
        ))}
      </RawContext.Provider>
      <div className="msg-foot">
        <button
          className="msg-info"
          title="message info"
          aria-label="message info"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          ⓘ
        </button>
      </div>
      {open && (
        <MessageMeta meta={message.meta} raw={raw} onToggleRaw={setRaw} hueForModel={hueForModel} />
      )}
    </div>
  )
}
