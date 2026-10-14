import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { describe, expect, it } from 'vitest'

const source = readFileSync(resolve(__dirname, '../../src/treg/web/adtrack.js'), 'utf8')
function page(url: string, cookie = '', slug = '', storage: any = new Map<string, string>()) {
  const events: Record<string, (e: any) => void> = {}
  const document = {
    cookie, baseURI: url, body: { dataset: { page: slug } },
    addEventListener: (name: string, fn: any) => { events[name] = fn },
  }
  let address = url
  const state = { spa: true }
  const window = { sessionStorage: {
    getItem: (key: string) => storage.get(key),
    setItem: (key: string, value: string) => storage.set(key, value),
  }, location: new URL(url), history: {
    state, replaceState: (s: any, _: string, href: string) => { expect(s).toBe(state); address = href },
  } }
  runInNewContext(source, { window, document, URL })
  return {
    document, address,
    click(href: string, event = 'click') {
      const a = { href, getAttribute: () => href }
      events[event]?.({ target: { closest: () => a } })
      return a.href
    },
  }
}

describe('ad click carry', () => {
  it.each(['gclid', 'gbraid', 'wbraid'])('captures %s, keeps landing across a new browser and a different page', kind => {
    const first = page(`https://treg.test/?${kind}=NEW`, 'treg_ad=gclid%7COLD%7Cold', 'original')
    expect(first.document.cookie).toContain(encodeURIComponent(`${kind}|NEW|original`))
    const dest = first.click('/login')
    expect(new URL(dest).searchParams.get(kind)).toBe('NEW')
    const next = page(dest, '', 'destination')
    expect(next.document.cookie).toContain(encodeURIComponent(`${kind}|NEW|original`))
    expect(new URL(first.address).searchParams.get('utm_content')).toBe('original')
  })
  it('keeps cookie-only return visits off URLs and leaves expiry unchanged', () => {
    for (const cookie of ['treg_ad=gbraid%7CPHONE%7Coriginal', 'treg_ad=PHONE%7Coriginal']) {
      const p = page('https://treg.test/login', cookie)
      expect(p.document.cookie).toBe(cookie)
      expect(p.address).toBe('https://treg.test/login')
      expect(p.click('/start', 'auxclick')).toBe('/start')
    }
  })
  it('carries a cookie during the same ad tab session, but not a different click', () => {
    const storage = new Map<string, string>()
    page('https://treg.test/?gbraid=PHONE', '', 'original', storage)
    const cookie = 'treg_ad=gbraid%7CPHONE%7Coriginal'
    const next = page('https://treg.test/login', cookie, '', storage)
    expect(next.address).toContain('gbraid=PHONE')
    expect(next.click('/start', 'auxclick')).toContain('utm_content=original')
    expect(next.document.cookie).toBe(cookie)
    const changed = page('https://treg.test/login', 'treg_ad=gclid%7COTHER%7Coriginal', '', storage)
    expect(changed.address).toBe('https://treg.test/login')
  })
  it('survives blocked session storage, carrying only a direct URL click', () => {
    const blocked = { get() { throw new Error('blocked') }, set() { throw new Error('blocked') } }
    expect(page('https://treg.test/?gclid=PHONE', '', '', blocked).click('/login')).toContain('gclid=PHONE')
    expect(page('https://treg.test/', 'treg_ad=gclid%7CPHONE%7C', '', blocked).click('/login')).toBe('/login')
  })
  it('leaves excluded links untouched including empty existing click params', () => {
    const p = page('https://treg.test/?gclid=TEST')
    for (const href of ['https://external.test/', 'mailto:a@b.com', '#section', '/x?gbraid=', '/x?wbraid=OTHER']) {
      expect(p.click(href)).toBe(href)
    }
    expect(p.click('/dynamic')).toContain('gclid=TEST')
    expect(p.address).not.toContain('utm_content')
  })
  it('ignores malformed cookies and stays inert without attribution', () => {
    for (const cookie of ['', 'treg_ad=%E0%A4%A', 'treg_ad=junk']) {
      const p = page('https://treg.test/', cookie)
      expect(p.address).toBe('https://treg.test/')
      expect(p.click('/login')).toBe('/login')
    }
  })
})
