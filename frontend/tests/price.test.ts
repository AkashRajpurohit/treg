import { expect, test } from 'vitest'
import catalog from '../src/state/catalog.js'

const label = (c: object) => catalog.costLabel.call(catalog, c)

test('a routed row shows its children\'s range, never its $0 floor as free', () => {
  const routed = { type: 'per_success', value: 0, currency: 'USD', usd: 0, range_usd: [0, 0.3], misses_billed: true }
  expect(label(routed)).toBe('$0-\u200b$0.3/success, misses billed')
  expect(label({ ...routed, misses_billed: undefined, range_usd: [0.0048, 0.75] })).toBe('$0.0048-\u200b$0.75/success')
})

test('quota rows are priced per result, the way they reserve and settle', () => {
  expect(label({ type: 'quota_rows', value: 1, currency: 'unit', unit: 'quota_row', usd: 0.00667 })).toBe('$0.0067/result')
})
