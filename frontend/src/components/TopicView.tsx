import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type TopicDetail, type TopicLink } from '../api'
import { Markdown } from './Markdown'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

// A link row reads from this topic's side: outgoing keeps the stored type
// ("implements →"), incoming flips the arrow ("← implements").
function linkLabel(l: TopicLink): string {
  return l.direction === 'out' ? `${l.link_type} →` : `← ${l.link_type}`
}

export function TopicView() {
  const { id } = useParams()
  const navigate = useNavigate()
  const topicId = id ? parseInt(id, 10) : NaN
  const [data, setData] = useState<TopicDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (isNaN(topicId)) return
    setData(null)
    setErr(null)
    api.topic(topicId).then(setData).catch((e) => setErr(String(e.message ?? e)))
  }, [topicId])

  if (isNaN(topicId)) return <div className="wrap"><div className="empty">bad topic id</div></div>
  if (err) return <div className="wrap"><div className="empty">topic unavailable: {err}</div></div>
  if (!data) return <div className="wrap"><div className="empty">loading…</div></div>

  const openOther = (l: TopicLink) =>
    navigate(l.other_type === 'topic' ? '/topic/' + l.other_id : '/archive/' + l.other_id)

  // The hierarchy strip: parents and children read straight off the links the
  // server already returns (part-of is child→parent, contains parent→child).
  const isTopic = (l: TopicLink) => l.other_type === 'topic'
  const parents = data.links.filter(
    (l) => isTopic(l) && ((l.direction === 'out' && l.link_type === 'part-of') || (l.direction === 'in' && l.link_type === 'contains')),
  )
  const children = data.links.filter(
    (l) => isTopic(l) && ((l.direction === 'in' && l.link_type === 'part-of') || (l.direction === 'out' && l.link_type === 'contains')),
  )

  return (
    <div className="wrap">
      <h1 className="topic-title">{data.title || 'topic ' + data.id}</h1>
      <div className="submeta meta">
        {data.topic_kind && <span className="badge">{data.topic_kind}</span>}
        {data.archived && <span className="badge">archived</span>}
        {data.graph && <span className="badge">{data.graph.degree} link{data.graph.degree === 1 ? '' : 's'}</span>}
        {data.graph?.community != null && <span className="badge">community {data.graph.community}</span>}
        {data.created_at && <span>created {fmtDate(data.created_at)}</span>}
      </div>
      {(parents.length > 0 || children.length > 0) && (
        <div className="hier">
          {parents.length > 0 && (
            <div className="hier-row">
              <span className="hier-label">part of</span>
              <span className="chips">
                {parents.map((p) => (
                  <button className="chip" key={p.other_id} onClick={() => navigate('/topic/' + p.other_id)}>
                    {p.other_title || 'topic ' + p.other_id}
                  </button>
                ))}
              </span>
            </div>
          )}
          {children.length > 0 && (
            <div className="hier-row">
              <span className="hier-label">contains</span>
              <span className="chips">
                {children.map((c) => (
                  <button className="chip" key={c.other_id} onClick={() => navigate('/topic/' + c.other_id)}>
                    {c.other_title || 'topic ' + c.other_id}
                  </button>
                ))}
              </span>
            </div>
          )}
        </div>
      )}
      {data.description && <Markdown>{data.description}</Markdown>}
      {data.summary && (
        <div className="group">
          <div className="gh"><span>summary</span></div>
          <Markdown>{data.summary}</Markdown>
        </div>
      )}

      {data.links.length > 0 && (
        <div className="group">
          <div className="gh">
            <span>links</span>
            <span className="src">{data.links.length}</span>
          </div>
          {data.links.map((l) => (
            <button className="hit" key={`${l.direction}:${l.other_id}:${l.link_type}`} onClick={() => openOther(l)}>
              <div className="snip">
                <span className="badge">{linkLabel(l)}</span>{' '}
                <strong>{l.other_title || `thread ${l.other_id}`}</strong>
                {l.evidence ? ` — ${l.evidence}` : ''}
              </div>
              <div className="meta">
                <span className="badge">{l.other_type}</span>
                <span className="badge">strength {l.strength.toFixed(2)}</span>
              </div>
            </button>
          ))}
        </div>
      )}

      {data.evidence.length > 0 && (
        <div className="group">
          <div className="gh">
            <span>citations</span>
            <span className="src">{data.evidence.length}</span>
          </div>
          {data.evidence.map((ev) => (
            <button
              className="hit"
              key={ev.event_id}
              onClick={() => navigate(`/archive/${ev.thread_id}?e=${ev.event_id}`)}
            >
              <div className="snip">“{ev.quote}”</div>
              <div className="meta">
                <span className="badge">{ev.thread_title || 'thread ' + ev.thread_id}</span>
                {ev.created_at && <span className="badge">{fmtDate(ev.created_at)}</span>}
              </div>
            </button>
          ))}
        </div>
      )}

      {data.peers.length > 0 && (
        <div className="group">
          <div className="gh"><span>community peers</span></div>
          <div className="chips">
            {data.peers.map((p) => (
              <button className="chip" key={p.thread_id} onClick={() => navigate('/topic/' + p.thread_id)}>
                {p.title || 'topic ' + p.thread_id}
              </button>
            ))}
          </div>
        </div>
      )}

      {data.links.length === 0 && data.evidence.length === 0 && (
        <div className="empty">no links or citations yet</div>
      )}
    </div>
  )
}
