import { useCallback, useEffect, useRef, useState } from 'react'
import { Pause, Play, Volume2, VolumeX } from 'lucide-react'
import type { BeaconSettings } from './types'
import { beaconActive, beaconTime, crossfadeStartTime, fadeGain, LOOP_CROSSFADE_SECONDS, loopGains, shouldStartCrossfade } from './workshop-timing'

interface Props {
  src: string
  label: string
  beacon?: BeaconSettings
  beaconSrc?: string
}

export function WorkshopPlayer({ src, label, beacon, beaconSrc }: Props) {
  const voice = useRef<HTMLAudioElement>(null)
  const context = useRef<AudioContext | null>(null)
  const beds = useRef<HTMLAudioElement[]>([])
  const gains = useRef<GainNode[]>([])
  const active = useRef(0)
  const crossing = useRef(false)
  const frame = useRef<number>(0)
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [duration, setDuration] = useState(0)
  const [message, setMessage] = useState('')
  const enabled = !!beacon?.enabled && !!beacon?.asset_id && !!beaconSrc

  const pause = useCallback((reason = '') => {
    voice.current?.pause()
    beds.current.forEach((bed) => bed.pause())
    window.cancelAnimationFrame(frame.current)
    setPlaying(false)
    if (reason) setMessage(reason)
  }, [])

  useEffect(() => {
    pause()
    setPosition(0)
    setDuration(0)
    setMessage('')
  }, [src, beaconSrc, pause])

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
    pair.forEach((bed) => {
      bed.addEventListener('waiting', onFailure)
      bed.addEventListener('stalled', onFailure)
      bed.addEventListener('error', onFailure)
    })
    context.current = audioContext
    beds.current = pair
    gains.current = nodes
    active.current = 0
    crossing.current = false
    return () => {
      pair.forEach((bed) => {
        bed.pause()
        bed.removeEventListener('waiting', onFailure)
        bed.removeEventListener('stalled', onFailure)
        bed.removeEventListener('error', onFailure)
        bed.src = ''
      })
      nodes.forEach((gain) => gain.disconnect())
      void audioContext.close()
      context.current = null
      beds.current = []
      gains.current = []
    }
  }, [enabled, beaconSrc, pause])

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
    if (!lead.paused && first.paused) void first.play().catch(() => pause('Beacon no pudo reproducirse.'))
    if (shouldStartCrossfade(lead.paused, crossing.current, nextGain)) {
      crossing.current = true
      second.currentTime = crossfadeStartTime(lead.currentTime, beacon.offset_seconds, bedDuration)
      void second.play().catch(() => pause('Beacon no pudo continuar el bucle.'))
    }
    if (crossing.current && first.currentTime >= bedDuration - 0.015) {
      first.pause()
      first.currentTime = 0
      active.current = 1 - active.current
      crossing.current = false
    }
  }, [beacon, enabled, pause])

  useEffect(() => {
    if (!playing) return
    const tick = () => {
      if (voice.current) setPosition(voice.current.currentTime)
      align()
      frame.current = window.requestAnimationFrame(tick)
    }
    frame.current = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame.current)
  }, [align, playing])

  const toggle = async () => {
    const lead = voice.current
    if (!lead) return
    if (playing) return pause()
    setMessage('')
    try {
      if (enabled) {
        await context.current?.resume()
        if (!beds.current[0]?.readyState || !beds.current[0]?.duration) throw new Error('Beacon todavía está cargando.')
      }
      await lead.play()
      align(true)
      setPlaying(true)
    } catch (error) {
      pause((error as Error).message)
    }
  }

  const seek = (value: number) => {
    if (!voice.current) return
    voice.current.currentTime = value
    setPosition(value)
    align(true)
  }

  return <div className="workshop-player">
    <audio ref={voice} src={src} preload="metadata" onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)} onEnded={() => pause()} onWaiting={() => pause('Audio en buffering. Reproducción pausada.')} onStalled={() => pause('Audio detenido. Reproducción pausada.')} onError={() => pause('Audio no disponible.')} />
    <button type="button" className="icon-button" title={playing ? 'Pausar preview' : 'Reproducir preview'} aria-label={playing ? 'Pausar preview' : 'Reproducir preview'} onClick={() => void toggle()}>{playing ? <Pause size={16} /> : <Play size={16} />}</button>
    <div className="workshop-player-main"><strong>{label}</strong><input aria-label="Posición del preview" type="range" min="0" max={duration || 1} step="0.01" value={Math.min(position, duration || 1)} onChange={(event) => seek(Number(event.target.value))} /></div>
    <span className="workshop-player-time">{Math.floor(position / 60)}:{String(Math.floor(position % 60)).padStart(2, '0')}</span>
    {enabled ? <span className="workshop-player-beacon" title="Beacon sincronizado"><Volume2 size={14} /> Beacon</span> : <span className="workshop-player-beacon muted" title="Sin Beacon"><VolumeX size={14} /></span>}
    {message && <span className="workshop-player-error" role="alert">{message}</span>}
  </div>
}
