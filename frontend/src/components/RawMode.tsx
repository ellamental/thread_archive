import { createContext, useContext } from 'react'

// When on, text blocks render as raw source (a <pre>) instead of rendered
// markdown — the "view as raw" toggle in the info drawer. A context so the deep
// ThreadView → Message → BlockView → Markdown chain reads it without prop-drilling.
export const RawContext = createContext(false)

export const useRaw = () => useContext(RawContext)
