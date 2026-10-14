import { expect, test } from 'vitest'
import billing from '../src/state/billing.js'

const glance = (micro: number) => billing.moneyGlance.call({ money: billing.money }, micro)

test('the top bar balance shows cents and never rounds up', () => {
  expect(glance(24_485_672)).toBe('$24.48')
  expect(glance(24_999_999)).toBe('$24.99')
  expect(glance(1_000_000)).toBe('$1.00')
})

test('a balance under a cent says so rather than reading as nothing', () => {
  expect(glance(4_200)).toBe('<$0.01')
  expect(glance(0)).toBe('$0.00')
  expect(glance(-1_234_567)).toBe('-$1.23')
})

test('the Usage spend card reads to the cent, never rounded up, and shows any spend under a cent', () => {
  expect(glance(228_470_706)).toBe('$228.47')   // was $228.470706
  expect(glance(4_200)).toBe('<$0.01')
  expect(glance(0)).toBe('$0.00')
})
