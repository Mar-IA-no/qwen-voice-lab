import { describe, expect, it } from 'vitest'
import { beaconActive, beaconTime, crossfadeStartTime, effectiveSpeed, fadeGain, loopGains, newBlock, shouldPauseBeaconBuffering, shouldStartCrossfade, validBlocks } from './workshop-timing'

describe('workshop timing', () => {
  it('inherits general speed only for null block speed, and includes take baseline', () => {
    expect(effectiveSpeed({ speed: null }, 1.25, 0.98)).toBeCloseTo(1.225)
    expect(effectiveSpeed({ speed: 0.8 }, 1.25, 0.98)).toBeCloseTo(0.784)
  })

  it('keeps stable block ids and accepts long pauses', () => {
    expect(newBlock().id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    expect(validBlocks([{ id: 'a', text: 'One', pause_after_ms: 120_000, speed: null }])).toBe(true)
    expect(validBlocks([{ id: 'a', text: 'One', pause_after_ms: 0, speed: null }, { id: 'a', text: 'Two', pause_after_ms: 0, speed: 1 }])).toBe(false)
  })

  it('maps shared transport offset, fade, and loop crossfade', () => {
    expect(beaconActive(1, 2)).toBe(false)
    expect(beaconTime(2, 2, 10)).toBe(0)
    expect(beaconTime(12.5, 2, 10)).toBeCloseTo(0.7)
    expect(beaconTime(0, -2, 10)).toBe(2)
    expect(fadeGain(1.5, 0)).toBeCloseTo(0.5)
    expect(fadeGain(3, 0)).toBe(1)
    expect(loopGains(9.9, 10)[0]).toBeCloseTo(0.5)
    expect(loopGains(9.9, 10)[1]).toBeCloseTo(0.5)
  })

  it('uses overlap-aware loop periods without accumulated seek drift', () => {
    const period = 10 - 0.2
    expect(beaconTime(10, 0, 10)).toBeCloseTo(0.2)
    expect(beaconTime(2 * period + 0.15, 0, 10)).toBeCloseTo(0.15)
    expect(beaconTime(20, 0, 10)).toBeCloseTo(0.4)
    expect(crossfadeStartTime(9.9, 0, 10)).toBeCloseTo(0.1)
    expect(crossfadeStartTime(19.7, 0, 10)).toBeCloseTo(0.1)
    expect(beaconTime(5.05, 1, 0.1)).toBeCloseTo(0.05)
  })

  it('does not start the crossfade bed from a paused seek', () => {
    expect(shouldStartCrossfade(true, false, 0.5)).toBe(false)
    expect(shouldStartCrossfade(false, false, 0.5)).toBe(true)
    expect(shouldStartCrossfade(false, true, 0.5)).toBe(false)
  })

  it('does not cancel initial Beacon loading or a pending first play, but pauses buffering after audible playback', () => {
    expect(shouldPauseBeaconBuffering(true, false, false)).toBe(false)
    expect(shouldPauseBeaconBuffering(false, true, false)).toBe(false)
    expect(shouldPauseBeaconBuffering(false, true, true)).toBe(false)
    expect(shouldPauseBeaconBuffering(false, false, false)).toBe(false)
    expect(shouldPauseBeaconBuffering(false, false, true)).toBe(true)
    expect(shouldPauseBeaconBuffering(true, false, true)).toBe(false)
  })
})
