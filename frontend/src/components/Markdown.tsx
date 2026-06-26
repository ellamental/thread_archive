import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'

// Assistant/user text is markdown (GFM); fenced code is syntax-highlighted via
// highlight.js (rehype-highlight) with the github-dark theme imported in main.tsx.
export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
