import { useState } from 'react'
import { AskUserQuestionView, askQuestions } from './AskQuestion'
import { Markdown } from './Markdown'
import { RawContext } from './RawMode'
import { colorStyle, modelColor } from '../modelColor'
import type { ModelColor } from '../modelColor'
import type { AskQuestion, Block, BlockImage, Message as Msg, MessageMeta } from '../api'

function prettyInput(input: unknown): string {
  if (typeof input === 'string') return input
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

function fmtBytes(n: number | null): string {
  if (n == null || n < 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

// Binary content attached to a block: images render inline (click-through to the
// full-size blob); non-image media (PDFs etc.) render as a download link. A
// pointer-only ref (bytes never in the archive) gets a labelled stub.
function BlockImages({ images }: { images?: BlockImage[] }) {
  if (!images || images.length === 0) return null
  return (
    <div className="block-images">
      {images.map((img, i) => {
        const size = fmtBytes(img.bytes)
        if (!img.url) {
          return (
            <span className="block-image-stub" key={i} title={img.pointer ?? undefined}>
              {img.kind}
              {size ? ` · ${size}` : ''} · not archived
            </span>
          )
        }
        if (img.kind === 'image') {
          return (
            <a href={img.url} target="_blank" rel="noreferrer" key={i}>
              <img
                className="block-image"
                src={img.url}
                loading="lazy"
                alt={img.media_type ?? 'image'}
              />
            </a>
          )
        }
        return (
          <a className="block-image-stub" href={img.url} target="_blank" rel="noreferrer" key={i}>
            {img.kind}
            {size ? ` · ${size}` : ''}
          </a>
        )
      })}
    </div>
  )
}

function BlockView({ block }: { block: Block }) {
  switch (block.type) {
    case 'text':
      return (
        <>
          {block.text && <Markdown>{block.text}</Markdown>}
          <BlockImages images={block.images} />
        </>
      )
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
      // An answered question whose call block didn't survive into this message
      // (a message split landed between them) still renders as the decision it is,
      // off the answer's own copy of the questions.
      if (block.answers)
        return <AskUserQuestionView questions={block.questions ?? []} answers={block.answers} />
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">
              result{block.truncated ? ' · truncated' : ''}
              {block.images?.length ? ` · ${block.images.length} image${block.images.length > 1 ? 's' : ''}` : ''}
            </span>
          </summary>
          {block.output && <pre className="tool-body">{block.output}</pre>}
          <BlockImages images={block.images} />
        </details>
      )
    case 'tool_error':
      return (
        <details className="tool error" open>
          <summary>
            <span className="tool-tag">tool error</span>
          </summary>
          {block.error && <pre className="tool-body">{block.error}</pre>}
          <BlockImages images={block.images} />
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
    case 'hook':
      // A hook that fired and injected content — the hook's name up front,
      // the injected text in the fold. <pre>, not markdown: injections are
      // plain text whose indentation markdown would mangle.
      return (
        <details className="tool hook">
          <summary>
            <span className="tool-name">{block.hook_name}</span>
            <span className="tool-tag">hook</span>
          </summary>
          <pre className="tool-body">{block.text}</pre>
        </details>
      )
    case 'attachment':
      return (
        <details className="tool">
          <summary>
            <span className="tool-tag">attachment · {block.attachment_type}</span>
          </summary>
          <pre className="tool-body">{block.text || '(no content)'}</pre>
        </details>
      )
    case 'model_switch':
      // A first-class model switch marker, styled as a centered divider like the
      // harness's own transcript. A user /model switch shows just the target; a
      // safeguard fallback shows from → to.
      return (
        <div className="model-switch">
          <span className="model-switch-label">
            {block.from_model && block.to_model ? (
              <>
                model switch · <b>{block.from_model}</b> → <b>{block.to_model}</b>
              </>
            ) : (
              <>
                switched to <b>{block.to_model}</b>
              </>
            )}
          </span>
        </div>
      )
    case 'safeguard_notice':
      // The reason for the switch — shown open (not folded), since it explains the jump.
      return (
        <div className="safeguard">
          <span className="safeguard-tag">⚠ safeguard</span>
          <Markdown>{block.text}</Markdown>
        </div>
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

// One renderable item: a regular block, a run of consecutive hook_fired markers
// merged into a single compact row (a hook can fire around every tool call, and a
// chip row reads where a details-box per firing would drown the turn), or an
// AskUserQuestion call folded together with the answer it got.
type RenderItem =
  | { kind: 'block'; block: Block }
  | { kind: 'hook_fires'; names: string[] }
  | { kind: 'ask'; questions: AskQuestion[]; answers?: Record<string, string>; dismissed: boolean }

// The block that says how an AskUserQuestion call turned out — the next block
// that isn't a hook marker, since hooks fire around the call without separating it
// from its result.
function outcomeIndex(blocks: Block[], from: number): number {
  let i = from
  while (i < blocks.length && blocks[i].type === 'hook_fired') i++
  return i
}

function groupBlocks(blocks: Block[]): RenderItem[] {
  const items: RenderItem[] = []
  const folded = new Set<number>()
  blocks.forEach((b, i) => {
    if (folded.has(i)) return
    if (b.type === 'hook_fired') {
      const last = items[items.length - 1]
      if (last?.kind === 'hook_fires') last.names.push(b.hook_name)
      else items.push({ kind: 'hook_fires', names: [b.hook_name] })
      return
    }
    if (b.type === 'tool_use' && b.name === 'AskUserQuestion') {
      const questions = askQuestions(b.input)
      if (questions) {
        // Fold the outcome into the question card: the answer marks the option that
        // won, and a denial reads as "dismissed" — either way the harness's prose
        // recap of what was already rendered above is dropped, not shown twice.
        const j = outcomeIndex(blocks, i + 1)
        const outcome = blocks[j]
        let answers: Record<string, string> | undefined
        let dismissed = false
        if (outcome?.type === 'tool_result' && outcome.answers) {
          answers = outcome.answers
          folded.add(j)
        } else if (outcome?.type === 'tool_error' && outcome.denial_kind) {
          dismissed = true
          folded.add(j)
        }
        items.push({ kind: 'ask', questions, answers, dismissed })
        return
      }
    }
    items.push({ kind: 'block', block: b })
  })
  return items
}

function HookFires({ names }: { names: string[] }) {
  // Dedup in first-seen order, counting repeats ("PreToolUse:Read ×2").
  const counts = new Map<string, number>()
  for (const n of names) counts.set(n, (counts.get(n) ?? 0) + 1)
  return (
    <div className="hook-fires">
      <span className="hook-fires-label">hooks</span>
      {[...counts.entries()].map(([n, c]) => (
        <span className="hook-fire" key={n}>
          {n}
          {c > 1 ? ` ×${c}` : ''}
        </span>
      ))}
    </div>
  )
}

// Compact token count ("12.6k").
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
// turn's api_request events) plus a "view as raw" toggle. Inline turn-meta —
// under the message, not a side sheet.
function MessageMeta({
  meta,
  raw,
  onToggleRaw,
  hueForModel,
}: {
  meta?: MessageMeta
  raw: boolean
  onToggleRaw: (v: boolean) => void
  hueForModel?: Record<string, ModelColor>
}) {
  const models = meta?.models ?? []
  const tok = meta?.tokens
  const time = fmtTime(meta?.ts)
  return (
    <div className="msg-meta">
      <div className="msg-meta-row">
        {models.map((m) => (
          <span className="chip model-chip" key={m} style={colorStyle(hueForModel?.[m] ?? modelColor(m))}>
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

// Copy this message's deep link (the same ?e= form search hits use). The copied
// URL is absolute — it leaves the page, so a relative path would be useless.
function PermalinkButton({ permalink }: { permalink: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(new URL(permalink, window.location.href).toString())
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard unavailable (permissions, non-secure context) — nothing to show
    }
  }
  return (
    <button
      className={'msg-link' + (copied ? ' copied' : '')}
      title="copy link to this message"
      aria-label="copy link to this message"
      onClick={copy}
    >
      {copied ? '✓ copied' : '🔗'}
    </button>
  )
}

export function Message({
  message,
  hueForModel,
  continued,
  highlighted,
  findTarget,
  anchorId,
  permalink,
}: {
  message: Msg
  hueForModel?: Record<string, ModelColor>
  // This message continues the previous one's group (same role, same model) — one
  // inference in an ongoing tool loop. Continuations drop the role label and merge
  // into the panel above, so a same-model turn reads as one stream and only a model
  // switch or role change starts a fresh, labelled bubble.
  continued?: boolean
  // This message holds the search hit the reader arrived by (?e= deep link) —
  // accented so the eye lands on the matching turn, not the top of the thread.
  highlighted?: boolean
  // The active match from the reader's local find control.
  findTarget?: boolean
  anchorId?: string
  // The in-app path deep-linking this message (/archive/<id>?e=<event>); when
  // present the footer offers a copy-link button.
  permalink?: string
}) {
  const [open, setOpen] = useState(false)
  const [raw, setRaw] = useState(false)
  // A manual /model switch is its own "message" but renders as a bare between-turns
  // divider — no bubble, role label, or info drawer.
  if (message.role === 'model_switch') {
    return (
      <div className="msg-divider">
        {message.blocks.map((b, i) => (
          <BlockView key={i} block={b} />
        ))}
      </div>
    )
  }
  // Tint an assistant message to match the model that produced it (this inference's
  // model — one per message now). Other roles carry no model, so no accent.
  const model = message.role === 'assistant' ? message.meta?.models?.[0] : undefined
  const color = model ? (hueForModel?.[model] ?? modelColor(model)) : undefined
  return (
    <div
      id={anchorId}
      className={
        'msg ' + message.role + (color ? ' has-model' : '') + (continued ? ' cont' : '') +
        (highlighted ? ' hit-target' : '') + (findTarget ? ' find-target' : '')
      }
      style={color ? colorStyle(color) : undefined}
    >
      {!continued && <div className="role">{message.role}</div>}
      <RawContext.Provider value={raw}>
        {groupBlocks(message.blocks).map((item, i) =>
          item.kind === 'hook_fires' ? (
            <HookFires key={i} names={item.names} />
          ) : item.kind === 'ask' ? (
            <AskUserQuestionView
              key={i}
              questions={item.questions}
              answers={item.answers}
              dismissed={item.dismissed}
            />
          ) : (
            <BlockView key={i} block={item.block} />
          ),
        )}
      </RawContext.Provider>
      <div className="msg-foot">
        {permalink && <PermalinkButton permalink={permalink} />}
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
