import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { useRaw } from './RawMode'

// Assistant/user text is markdown (GFM); fenced code is syntax-highlighted via
// highlight.js (rehype-highlight) with the github-dark theme imported in main.tsx.
// In raw mode (the drawer's "view as raw" toggle) the same text renders verbatim
// as a <pre> so the underlying markdown source is visible.
export function Markdown({ children }: { children: string }) {
  const raw = useRaw()
  if (raw) return <pre className="md-raw">{children}</pre>
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
