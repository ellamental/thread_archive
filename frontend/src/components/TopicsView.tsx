import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, type TopicListItem, type TopicsResponse, type TopicTreeNode, type TopicTreeResponse } from '../api'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

interface Community {
  id: number | null
  topics: TopicListItem[] // pagerank-ranked (server order)
}

// Group by community, communities ordered by summed pagerank (the heaviest
// cluster first); unlinked topics (community null) trail as their own group.
function communities(topics: TopicListItem[]): Community[] {
  const byId = new Map<number | null, Community>()
  for (const t of topics) {
    let c = byId.get(t.community)
    if (!c) {
      c = { id: t.community, topics: [] }
      byId.set(t.community, c)
    }
    c.topics.push(t)
  }
  const weight = (c: Community) => c.topics.reduce((s, t) => s + t.pagerank, 0)
  return [...byId.values()].sort((a, b) => {
    if ((a.id === null) !== (b.id === null)) return a.id === null ? 1 : -1
    return weight(b) - weight(a)
  })
}

function CommunitiesView() {
  const navigate = useNavigate()
  const [data, setData] = useState<TopicsResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  useEffect(() => {
    api.topics().then(setData).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  const shown = useMemo(() => {
    if (!data) return []
    const f = filter.trim().toLowerCase()
    const topics = f
      ? data.topics.filter(
          (t) => (t.title ?? '').toLowerCase().includes(f) || (t.description ?? '').toLowerCase().includes(f),
        )
      : data.topics
    return communities(topics)
  }, [data, filter])

  if (err) return <div className="empty">topics unavailable: {err}</div>
  if (!data) return <div className="empty">loading…</div>

  const g = data.graph
  return (
    <>
      <div className="submeta">
        {data.topics.length} topic{data.topics.length === 1 ? '' : 's'}
        {g.available && g.communities ? ` · ${g.communities} communities (${g.community_engine})` : ''}
      </div>
      <div className="topics-filter">
        <input
          className="input"
          type="search"
          placeholder="filter topics…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          autoComplete="off"
        />
      </div>
      {shown.length === 0 && <div className="empty">{data.topics.length ? 'no matches' : 'no topics curated yet'}</div>}
      {shown.map((c) => (
        <div className="group" key={c.id ?? 'unlinked'}>
          <div className="gh">
            <span>{c.id === null ? 'unlinked' : c.topics[0]?.title || `community ${c.id}`}</span>
            <span className="src">
              {c.id === null ? 'no links yet' : `community · ${c.topics.length} topic${c.topics.length > 1 ? 's' : ''}`}
            </span>
          </div>
          {c.topics.map((t) => (
            <button className="hit" key={t.id} onClick={() => navigate('/topic/' + t.id)}>
              <div className="snip">
                <strong>{t.title || 'topic ' + t.id}</strong>
                {t.description ? ` — ${t.description}` : ''}
              </div>
              <div className="meta">
                {t.topic_kind && <span className="badge">{t.topic_kind}</span>}
                {t.link_count > 0 && <span className="badge">{t.link_count} link{t.link_count > 1 ? 's' : ''}</span>}
                {t.evidence_count > 0 && (
                  <span className="badge">{t.evidence_count} citation{t.evidence_count > 1 ? 's' : ''}</span>
                )}
                {t.updated_at && <span className="badge">{fmtDate(t.updated_at)}</span>}
              </div>
            </button>
          ))}
        </div>
      ))}
    </>
  )
}

// One node of the hierarchy view. Roots start open; deeper levels start closed
// so a heavy subtree doesn't wall the page. The title is the link to the topic
// page; the caret is the only expand/collapse control, so the two don't fight.
function TreeNode({ node, depth }: { node: TopicTreeNode; depth: number }) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(depth === 0)
  const kids = node.children
  return (
    <div className="tree-node" style={{ ['--tree-depth' as string]: depth }}>
      <div className="tree-row">
        {kids.length > 0 ? (
          <button className="tree-caret" aria-label={open ? 'collapse' : 'expand'} onClick={() => setOpen(!open)}>
            {open ? '▾' : '▸'}
          </button>
        ) : (
          <span className="tree-caret leaf">·</span>
        )}
        <button className="tree-title" onClick={() => navigate('/topic/' + node.id)}>
          {node.title || 'topic ' + node.id}
        </button>
        {node.topic_kind && <span className="badge">{node.topic_kind}</span>}
        {kids.length > 0 && <span className="src">{kids.length}</span>}
      </div>
      {open && kids.map((c) => <TreeNode key={c.id} node={c} depth={depth + 1} />)}
    </div>
  )
}

function TreeView() {
  const [data, setData] = useState<TopicTreeResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.topicTree().then(setData).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  if (err) return <div className="empty">hierarchy unavailable: {err}</div>
  if (!data) return <div className="empty">loading…</div>
  if (data.roots.length === 0) return <div className="empty">no part-of / contains links curated yet</div>

  return (
    <>
      <div className="submeta">
        {data.topics_in_hierarchy} of {data.topics_total} topics in the hierarchy · {data.roots.length} roots
      </div>
      {data.roots.map((r) => (
        <TreeNode key={r.id} node={r} depth={0} />
      ))}
    </>
  )
}

export function TopicsView() {
  const [params, setParams] = useSearchParams()
  const view = params.get('view') === 'tree' ? 'tree' : 'communities'
  const setView = (v: string) => setParams(v === 'tree' ? { view: 'tree' } : {}, { replace: true })

  return (
    <div className="wrap">
      <div className="seg" role="tablist">
        <button className={'seg-btn' + (view === 'communities' ? ' active' : '')} onClick={() => setView('communities')}>
          communities
        </button>
        <button className={'seg-btn' + (view === 'tree' ? ' active' : '')} onClick={() => setView('tree')}>
          hierarchy
        </button>
      </div>
      {view === 'tree' ? <TreeView /> : <CommunitiesView />}
    </div>
  )
}
