import { useCallback, useEffect, useRef, useState } from 'react'
import { Pause, Play, Square, Volume2, VolumeX } from 'lucide-react'
import type { BeaconSettings } from './types'
import { beaconActive, beaconTime, crossfadeStartTime, fadeGain, LOOP_CROSSFADE_SECONDS, loopGains, shouldPauseBeaconBuffering, shouldStartCrossfade } from './workshop-timing'

interface Props {
  src: string
  label: string
  beacon?: BeaconSettings
  beaconSrc?: string
  onPosition?: (seconds: number) => void
  durationHint?: number
  showStop?: boolean
  autoPlayRequested?: boolean
}

export function WorkshopPlayer({ src, label, beacon, beaconSrc, onPosition, durationHint = 0, showStop = false, autoPlayRequested = false }: Props) {
  const voice = useRef<HTMLAudioElement>(null)
  const context = useRef<AudioContext | null>(null)
  const beds = useRef<HTMLAudioElement[]>([])
  const startingBeds = useRef(new Set<HTMLAudioElement>())
  const playedBeds = useRef(new Set<HTMLAudioElement>())
  const gains = useRef<GainNode[]>([])
  const active = useRef(0)
  const crossing = useRef(false)
  const frame = useRef<number>(0)
  const playGeneration = useRef(0)
  const autoAttempted = useRef(false)
  const tryAutoPlay = useRef<() => void>(() => {})
  const onPositionRef = useRef(onPosition)
  onPositionRef.current = onPosition
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [duration, setDuration] = useState(0)
  const [message, setMessage] = useState('')
  const enabled = !!beacon?.enabled && !!beacon?.asset_id && !!beaconSrc

  const pause = useCallback((reason = '', cancelAutoplay = true) => {
    if (cancelAutoplay) autoAttempted.current = true
    ++playGeneration.current
    voice.current?.pause()
    beds.current.forEach((bed) => bed.pause())
    window.cancelAnimationFrame(frame.current)
    setPlaying(false)
    if (reason) setMessage(reason)
  }, [])

  useEffect(() => {
    pause('', false)
    setPosition(0)
    setDuration(0)
    setMessage('')
    autoAttempted.current = false
    onPositionRef.current?.(0)
  }, [src, beaconSrc, pause])

  useEffect(() => {
    const element = voice.current
    return () => {
      ++playGeneration.current
      element?.pause()
      beds.current.forEach((bed) => bed.pause())
      window.cancelAnimationFrame(frame.current)
    }
  }, [])

  useEffect(() => {
    if (!enabled || !beaconSrc) return
    const audioContext = new AudioContext()
    const pair = [new Audio(beaconSrc), new Audio(beaconSrc)]
    const nodes = pair.map((bed) => {
      bed.preload = 'auto'
      bed.loop = false
      const gain = audioContext.createGain()
      gain.gain.value = 0
      audioContext.createMediaElementSource(bed).connect(gain).connect(audioContext.destination)
      return gain
    })
    const onFailure = () => pause('Beacon no está disponible. Reproducción pausada.')
    const onBuffering = (event: Event) => {
      const bed = event.currentTarget as HTMLAudioElement
      if (shouldPauseBeaconBuffering(voice.current?.paused ?? true, startingBeds.current.has(bed) || bed.seeking, playedBeds.current.has(bed))) pause('Beacon en buffering. Reproducción pausada.')
    }
    const onMetadata = () => tryAutoPlay.current()
    pair.forEach((bed) => {
      bed.addEventListener('waiting', onBuffering)
      bed.addEventListener('stalled', onBuffering)
      bed.addEventListener('error', onFailure)
      bed.addEventListener('loadedmetadata', onMetadata)
      bed.addEventListener('canplay', onMetadata)
    })
    context.current = audioContext
    beds.current = pair
    gains.current = nodes
    active.current = 0
    crossing.current = false
    return () => {
      pair.forEach((bed) => {
        bed.pause()
        bed.removeEventListener('waiting', onBuffering)
        bed.removeEventListener('stalled', onBuffering)
        bed.removeEventListener('error', onFailure)
        bed.removeEventListener('loadedmetadata', onMetadata)
        bed.removeEventListener('canplay', onMetadata)
        bed.src = ''
      })
      nodes.forEach((gain) => gain.disconnect())
      void audioContext.close()
      context.current = null
      beds.current = []
      gains.current = []
      startingBeds.current.clear()
      playedBeds.current.clear()
    }
  }, [enabled, beaconSrc, pause])

  const playBed = useCallback((bed: HTMLAudioElement, reason: string) => {
    if (startingBeds.current.has(bed)) return
    const token = playGeneration.current
    startingBeds.current.add(bed)
    void bed.play().then(() => {
      startingBeds.current.delete(bed)
      if (token !== playGeneration.current) bed.pause()
      else playedBeds.current.add(bed)
    }).catch(() => {
      startingBeds.current.delete(bed)
      if (token === playGeneration.current) pause(reason)
    })
  }, [pause])

  const align = useCallback((seek = false) => {
    const lead = voice.current
    if (!lead || !enabled || !beacon || beds.current.length !== 2) return
    const pair = beds.current
    const first = pair[active.current]
    const second = pair[1 - active.current]
    const bedDuration = first.duration
    if (!beaconActive(lead.currentTime, beacon.offset_seconds)) {
      pair.forEach((bed) => bed.pause())
      gains.current.forEach((gain) => { gain.gain.value = 0 })
      return
    }
    if (!(bedDuration > 0)) return
    first.loop = bedDuration <= LOOP_CROSSFADE_SECONDS
    const desired = beaconTime(lead.currentTime, beacon.offset_seconds, bedDuration)
    if (seek || (!crossing.current && first.currentTime < bedDuration - LOOP_CROSSFADE_SECONDS && Math.abs(first.currentTime - desired) > 0.25)) {
      first.currentTime = desired
      second.pause()
      crossing.current = false
    }
    const fade = fadeGain(lead.currentTime, beacon.offset_seconds) * beacon.volume
    const [mainGain, nextGain] = loopGains(first.currentTime, bedDuration)
    gains.current[active.current].gain.value = fade * mainGain
    gains.current[1 - active.current].gain.value = fade * nextGain
    if (!lead.paused && first.paused) playBed(first, 'Beacon no pudo reproducirse.')
    if (shouldStartCrossfade(lead.paused, crossing.current, nextGain)) {
      crossing.current = true
      second.currentTime = crossfadeStartTime(lead.currentTime, beacon.offset_seconds, bedDuration)
      playBed(second, 'Beacon no pudo continuar el bucle.')
    }
    if (crossing.current && first.currentTime >= bedDuration - 0.015) {
      first.pause()
      first.currentTime = 0
      active.current = 1 - active.current
      crossing.current = false
    }
  }, [beacon, enabled, playBed])

  useEffect(() => {
    if (!playing) return
    const tick = () => {
      if (voice.current) {
        setPosition(voice.current.currentTime)
        onPositionRef.current?.(voice.current.currentTime)
      }
      align()
      frame.current = window.requestAnimationFrame(tick)
    }
    frame.current = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame.current)
  }, [align, playing])

  const toggle = async (automatic = false) => {
    if (!automatic) autoAttempted.current = true
    const lead = voice.current
    if (!lead) return
    if (playing) return pause()
    const token = ++playGeneration.current
    setMessage('')
    try {
      if (enabled) {
        await context.current?.resume()
        if (token !== playGeneration.current) return
        if (!beds.current.every((bed) => bed.readyState >= 3 && bed.duration > 0) || beds.current.length !== 2) throw new Error('Beacon todavía está cargando.')
      }
      await lead.play()
      if (token !== playGeneration.current) { lead.pause(); return }
      align(true)
      setPlaying(true)
    } catch (error) {
      if (token === playGeneration.current) pause(automatic && (error as Error).name === 'NotAllowedError' ? 'Escucha lista. Pulsá Reproducir para empezar.' : (error as Error).message)
    }
  }

  tryAutoPlay.current = () => {
    if (!autoPlayRequested || autoAttempted.current || (voice.current?.readyState ?? 0) < 3 || (enabled && (beds.current.length !== 2 || !beds.current.every((bed) => bed.duration > 0 && bed.readyState >= 3)))) return
    autoAttempted.current = true
    void toggle(true)
  }
  useEffect(() => { tryAutoPlay.current() }, [enabled, duration, autoPlayRequested])

  const seek = (value: number) => {
    autoAttempted.current = true
    if (!voice.current) return
    voice.current.currentTime = value
    setPosition(value)
    onPositionRef.current?.(value)
    align(true)
  }

  const stop = () => {
    pause()
    if (voice.current) voice.current.currentTime = 0
    beds.current.forEach((bed) => { bed.currentTime = 0 })
    active.current = 0
    crossing.current = false
    setPosition(0)
    onPositionRef.current?.(0)
  }

  return <div className="workshop-player">
    <audio ref={voice} src={src} preload="metadata" onLoadedMetadata={(event) => { setDuration(event.currentTarget.duration); tryAutoPlay.current() }} onCanPlay={() => tryAutoPlay.current()} onTimeUpdate={(event) => { setPosition(event.currentTarget.currentTime); onPositionRef.current?.(event.currentTarget.currentTime) }} onEnded={() => pause()} onWaiting={() => { if (playing) pause('Audio en buffering. Reproducción pausada.') }} onStalled={() => { if (playing) pause('Audio detenido. Reproducción pausada.') }} onError={() => pause('Audio no disponible.')} />
    <button type="button" className="icon-button" title={playing ? 'Pausar preview' : 'Reproducir preview'} aria-label={playing ? 'Pausar preview' : 'Reproducir preview'} onClick={() => void toggle()}>{playing ? <Pause size={16} /> : <Play size={16} />}</button>
    {showStop && <button type="button" className="icon-button" title="Detener escucha" aria-label="Detener escucha" onClick={stop}><Square size={15} /></button>}
    <div className="workshop-player-main"><strong>{label}</strong><input aria-label="Posición del preview" type="range" min="0" max={duration || durationHint || 1} step="0.01" value={Math.min(position, duration || durationHint || 1)} onChange={(event) => seek(Number(event.target.value))} /></div>
    <span className="workshop-player-time">{Math.floor(position / 60)}:{String(Math.floor(position % 60)).padStart(2, '0')}</span>
    {enabled ? <span className="workshop-player-beacon" title="Beacon sincronizado"><Volume2 size={14} /> Beacon</span> : <span className="workshop-player-beacon muted" title="Sin Beacon"><VolumeX size={14} /></span>}
    {message && <span className="workshop-player-error" role="alert">{message}</span>}
  </div>
}
