import type { Place } from '../../lib/api.ts'

export interface TripPreset {
  id: string
  label: string
  detail: string
  origin: Place
  destination: Place
  load: string
}

export const TRIP_PRESETS: readonly TripPreset[] = [
  {
    id: 'dal-atl',
    label: 'Dallas → Atlanta',
    detail: 'About 800 mi on truck routing',
    origin: { name: 'Dallas, Texas, US', lat: 32.7767, lon: -96.797 },
    destination: { name: 'Atlanta, Georgia, US', lat: 33.749, lon: -84.388 },
    load: '42000',
  },
  {
    id: 'chi-den',
    label: 'Chicago → Denver',
    detail: 'About 1,000 mi across the Plains',
    origin: { name: 'Chicago, Illinois, US', lat: 41.8781, lon: -87.6298 },
    destination: { name: 'Denver, Colorado, US', lat: 39.7392, lon: -104.9903 },
    load: '40000',
  },
  {
    id: 'chi-dal',
    label: 'Chicago → Dallas',
    detail: 'Example corridor · about 970 mi',
    origin: { name: 'Chicago, Illinois, US', lat: 41.8781, lon: -87.6298 },
    destination: { name: 'Dallas, Texas, US', lat: 32.7767, lon: -96.797 },
    load: '42000',
  },
] as const

export function presetById(id: string): TripPreset | undefined {
  return TRIP_PRESETS.find((item) => item.id === id)
}
