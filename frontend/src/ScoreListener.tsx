import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { Download, Play, X } from 'lucide-react'
import { api } from './api'
import { WorkshopPlayer } from './WorkshopPlayer'
import { assertScorePreview, playingScoreBlock, selectedScoreKeys } from './score-playback'
import { scoreSeconds } from './score-workspace'
import type { ScorePreview, ScoreWorkspaceDetail } from './types'

export interface ScoreListenHandle {
  prepare: (keys?: string[]) => void
  invalidate: () => void
}

interface Props {
  detail: ScoreWorkspaceDetail
  dirty: boolean
  saving: boolean
  selectedKeys: string[]
  onPreview: (preview: ScorePreview | null) => void
  onActiveBlock: (key: string | null) => void
}

export const ScoreListener = forwardRef<ScoreListenHandle, Props>(function ScoreListener({ detail, dirty, saving, selectedKeys, onPreview, onActiveBlock }, ref) {
  const [preview, setPreview] = useState<ScorePreview | null>(null)
  const [preparing, setPreparing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [beaconEnabled, setBeaconEnabled] = useState(false)
  const [beaconVolume, setBeaconVolume] = useState(0.25)
  const sequence = useRef(0)
  const callbacks = useRef({ onPreview, onActiveBlock })
  callbacks.current = { onPreview, onActiveBlock }
  const selectionSignature = JSON.stringify(selectedKeys)
  const preparedSignature = useRef('')

  const invalidate = useCallback(() => {
    ++sequence.current
    setPreview(null)
    setPreparing(false)
    setError(null)
    callbacks.current.onPreview(null)
    callbacks.current.onActiveBlock(null)
  }, [])

  useEffect(() => { invalidate() }, [detail.workspace.id, detail.revision?.id, dirty, saving, selectionSignature, invalidate])
  useEffect(() => () => { ++sequence.current }, [])

  const prepare = useCallback(async (keys?: string[]) => {
    if (dirty || saving || !detail.revision || detail.workspace.status !== 'ready') return
    invalidate()
    const token = ++sequence.current
    const snapshot = detail
    try {
      selectedScoreKeys(snapshot, keys)
      setPreparing(true)
      const next = await api.previewScore(snapshot.workspace.id, snapshot.revision!.id, keys)
      if (token !== sequence.current) return
      assertScorePreview(next, snapshot, keys)
      preparedSignature.current = selectionSignature
      setPreview(next)
      setBeaconEnabled(!!next.beacon?.enabled && next.beacon.status === 'ready')
      setBeaconVolume(next.beacon?.volume ?? 0.25)
      callbacks.current.onPreview(next)
    } catch (reason) { if (token === sequence.current) setError((reason as Error).message) }
    finally { if (token === sequence.current) setPreparing(false) }
  }, [detail, dirty, saving, invalidate, selectionSignature])

  useImperativeHandle(ref, () => ({ prepare: (keys) => { void prepare(keys) }, invalidate }), [prepare, invalidate])

  const playable = !dirty && !saving && preview?.revision_id === detail.revision?.id && preparedSignature.current === selectionSignature ? preview : null
  const disabled = dirty || saving || preparing || detail.workspace.status !== 'ready'
  return <section className="score-listener" aria-label="Escucha de la partitura">
    <div className="score-listen-actions"><button type="button" className="soft-button" disabled={disabled} onClick={() => void prepare()}><Play size={15} /> Escuchar todo</button><button type="button" className="soft-button" disabled={disabled || selectedKeys.length === 0} onClick={() => void prepare(selectedKeys)}><Play size={15} /> Escuchar selección{selectedKeys.length > 0 ? ` · ${selectedKeys.length}` : ''}</button>{preparing && <button type="button" className="soft-button" onClick={invalidate}><X size={14} /> Cancelar escucha</button>}</div>
    <p className="score-listen-note">Escucha de edición: omite respuestas humanas y tramos variables.</p>
    {dirty && <p className="score-listen-state" role="status">Guardá los cambios para escucharlos.</p>}
    {!dirty && !playable && !preparing && <p className="score-listen-state">Elegí todo, marcá bloques para una selección o escuchá un bloque desde el guion.</p>}
    {preparing && <p className="score-listen-state" role="status">Preparando la escucha de esta versión…</p>}
    {error && <p className="score-listen-error" role="alert">No se pudo preparar la escucha: {error}</p>}
    {playable && <>
      <WorkshopPlayer key={playable.id} src={playable.audio_url} label={`Versión ${detail.revision?.number} · ${playable.selection_mode === 'full' ? 'Recorrido completo' : `${playable.source_keys.length} ${playable.source_keys.length === 1 ? 'bloque' : 'bloques'}`} · ${scoreSeconds(playable.total_samples / playable.sample_rate * 1000)}`} durationHint={playable.total_samples / playable.sample_rate} showStop autoPlayRequested onPosition={(seconds) => callbacks.current.onActiveBlock(playingScoreBlock(playable, seconds))} beacon={playable.beacon ? { enabled: beaconEnabled, asset_id: playable.id, offset_seconds: playable.beacon.offset_seconds, volume: beaconVolume } : undefined} beaconSrc={playable.beacon?.audio_url} />
      <div className="score-listen-settings">{playable.beacon && <><label className="score-beacon-toggle"><input type="checkbox" checked={beaconEnabled} disabled={playable.beacon.status !== 'ready'} onChange={(event) => setBeaconEnabled(event.target.checked)} /><span>Escuchar Beacon</span></label><label className="score-beacon-volume"><span>Volumen Beacon</span><input aria-label="Volumen Beacon" type="range" min="0" max="1" step="0.01" value={beaconVolume} disabled={!beaconEnabled} onChange={(event) => setBeaconVolume(Number(event.target.value))} /></label>{playable.beacon.status !== 'ready' && <span>Beacon fijado no disponible</span>}</>}<a className="download-button" href={playable.download_url} download><Download size={14} /> WAV</a><a className="download-button" href={playable.manifest_url} download><Download size={14} /> Manifest de esta escucha</a></div>
      <p className="score-listen-exact">Tiempos exactos de este audio · {playable.total_samples.toLocaleString('es')} muestras · {playable.sample_rate.toLocaleString('es')} Hz. {playable.selection_mode === 'selection' && 'La selección sigue el orden guardado y omite el silencio inicial.'}</p>
    </>}
  </section>
})
