import type { WorkshopBlock } from './types'

export const FADE_SECONDS = 3
export const LOOP_CROSSFADE_SECONDS = 0.2

export function effectiveSpeed(block: { speed?: number | null }, speechSpeed: number, baselineSpeed = 1): number {
  return baselineSpeed * (block.speed ?? speechSpeed)
}

export function beaconTime(voiceTime: number, offsetSeconds: number, duration: number): number {
  if (!(duration > 0)) return 0
  const elapsed = Math.max(0, voiceTime - offsetSeconds)
  const period = duration > LOOP_CROSSFADE_SECONDS ? duration - LOOP_CROSSFADE_SECONDS : duration
  return ((elapsed % period) + period) % period
}

export function shouldStartCrossfade(voicePaused: boolean, crossing: boolean, nextGain: number): boolean {
  return !voicePaused && !crossing && nextGain > 0
}

export function shouldPauseBeaconBuffering(voicePaused: boolean, starting: boolean, hasPlayed: boolean): boolean {
  return !voicePaused && !starting && hasPlayed
}

export function crossfadeStartTime(voiceTime: number, offsetSeconds: number, duration: number): number {
  return Math.min(LOOP_CROSSFADE_SECONDS, beaconTime(voiceTime, offsetSeconds, duration))
}

export function beaconActive(voiceTime: number, offsetSeconds: number): boolean {
  return voiceTime >= offsetSeconds
}

export function fadeGain(voiceTime: number, offsetSeconds: number): number {
  return Math.max(0, Math.min(1, (voiceTime - Math.max(0, offsetSeconds)) / FADE_SECONDS))
}

export function loopGains(position: number, duration: number): [number, number] {
  if (!(duration > LOOP_CROSSFADE_SECONDS) || position < duration - LOOP_CROSSFADE_SECONDS) return [1, 0]
  const second = Math.max(0, Math.min(1, (position - (duration - LOOP_CROSSFADE_SECONDS)) / LOOP_CROSSFADE_SECONDS))
  return [1 - second, second]
}

export function newBlock(): WorkshopBlock {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  const id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  return { id, text: '', pause_after_ms: 0, speed: null }
}

export function validBlocks(blocks: WorkshopBlock[]): boolean {
  return blocks.length > 0 && new Set(blocks.map((row) => row.id)).size === blocks.length && blocks.every((row) =>
    !!row.id && !!row.text.trim() && Number.isSafeInteger(row.pause_after_ms) && row.pause_after_ms >= 0 &&
    (row.speed === null || (Number.isFinite(row.speed) && row.speed >= 0.5 && row.speed <= 2)))
}
