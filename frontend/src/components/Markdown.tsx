import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { useRaw } from './RawMode'

// Assistant/user text is markdown (GFM); fenced code is syntax-highlighted via
// highlight.js (rehype-highlight) with the github-dark theme imported in main.tsx.
// In raw mode (the drawer's "view as raw" toggle) the same text renders verbatim
// as a <pre> so the underlying markdown source is visible.
const components: Components = {
  img({ node: _node, src, alt, ...props }) {
    // Message text is untrusted archive content. Only the archive's
    // content-addressed, same-origin blob endpoint may load automatically;
    // remote, data, and arbitrary relative image URLs remain inert text.
    if (!src?.startsWith('/api/blob/')) {
      return (
        <span className="md-image-blocked" role="note">
          image blocked{alt ? ` · ${alt}` : ''}
        </span>
      )
    }
    return <img src={src} alt={alt ?? ''} loading="lazy" {...props} />
  },
}

export function Markdown({
  children,
  className,
  components: extra,
}: {
  children: string
  /** Extra class on the wrapper, for text whose typography differs (the manual). */
  className?: string
  /**
   * Renderers layered over the defaults above. The manual passes its own `a` and
   * heading renderers — its links address other manual pages, which are routes
   * here, and its anchors need heading ids to land on. Transcript text passes
   * none: an archived message gets exactly the guarded defaults.
   */
  components?: Components
}) {
  const raw = useRaw()
  if (raw) return <pre className="md-raw">{children}</pre>
  return (
    <div className={className ? 'md ' + className : 'md'}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={extra ? { ...components, ...extra } : components}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
