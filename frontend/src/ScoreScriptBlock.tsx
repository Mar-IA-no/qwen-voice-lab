import type { ReactNode } from 'react'
import type { ScoreWorkspaceBlock } from './types'
import { scoreSeconds } from './score-workspace'

export function ScriptBlock({ block, number, controls }: { block: ScoreWorkspaceBlock; number: number; controls?: ReactNode }) {
  const source = block.source
  return <article className="score-script-block">
    <div className="score-script-number">{number}</div>
    <div><p className="score-spoken-text">{block.text}</p><div className="score-block-footer">{!controls && <span className="score-pause">Pausa después · {scoreSeconds(block.pause_after_ms)}</span>}<span className="score-selection-state">{source.validation_status === 'pass' ? 'Validación automática' : 'Selección provisional'}</span></div>
      {controls}
      {block.response_marker && <p className="score-response-marker">{block.response_marker}</p>}
      {block.status === 'unavailable' && <p className="score-unavailable" role="status">Fuente no disponible{block.unavailable_reason ? ` · ${block.unavailable_reason}` : ''}</p>}
      <details className="score-source"><summary>Fuente fijada</summary><dl><div><dt>Proyecto / bloque</dt><dd>{source.project_id} / {source.segment_id}</dd></div><div><dt>Revisión editorial</dt><dd>{source.revision_id}</dd></div><div><dt>Toma / revisión de origen</dt><dd>{source.take_id} / {source.take_revision_id ?? 'No disponible'}</dd></div><div><dt>Audio SHA-256</dt><dd>{source.audio_sha256}</dd></div>{source.selection_override_reason && <div><dt>Motivo de selección</dt><dd>{source.selection_override_reason}</dd></div>}</dl></details>
    </div>
  </article>
}
