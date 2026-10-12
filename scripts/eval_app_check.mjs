// Browser checks for scripts/eval_app.py: opens the built app, types a keyword
// and reports what happened as JSON. It looks at behaviour (requests, links,
// console), not at class names, so it works on whatever the agent built.
//
//   node eval_app_check.mjs <playwright dir> <url> <keyword> [chrome|chromium]
import { createRequire } from 'node:module'

const [, , pwDir, url, keyword, channel = 'chrome'] = process.argv
const { chromium } = createRequire(pwDir + '/')('playwright')

const API = /hn\.algolia\.com|hacker-news\.firebaseio\.com/
const out = { loaded: false, stories: 0, input: false, errors: [], requests: [], missing: [] }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const browser = await chromium.launch({ headless: true, channel: channel === 'chrome' ? 'chrome' : undefined })
try {
  const page = await browser.newPage()
  page.on('console', (m) => m.type() === 'error' && out.errors.push(m.text().slice(0, 300)))
  page.on('pageerror', (e) => out.errors.push(String(e).slice(0, 300)))
  // Which file a "Failed to load resource" console error was about.
  page.on('response', (r) => r.status() >= 400 && !API.test(r.url()) && out.missing.push(`${r.status()} ${new URL(r.url()).pathname}`))
  page.on('response', (r) => API.test(r.url()) && out.requests.push({ url: r.url(), status: r.status(), at: Date.now() }))

  // Story links: anything that leads off the page, as a set of hrefs.
  const links = () => page.$$eval('a[href^="http"]', (as) => as.filter((a) => a.offsetParent !== null).map((a) => a.href))
  const settle = async (ms) => { try { await page.waitForLoadState('networkidle', { timeout: ms }) } catch { /* still loading */ } }

  await page.goto(url, { waitUntil: 'load', timeout: 20000 })
  await settle(15000)
  await sleep(500)
  const before = await links()
  out.loaded = true
  out.stories = new Set(before).size
  out.initial = out.requests.map((r) => r.url)
  // "Front page" is Algolia's front_page tag, or Firebase's top stories.
  out.front_page = out.initial.some((u) => /front_page|topstories/.test(u))

  const box = page.locator('input[type="text"], input[type="search"], input:not([type])').first()
  out.input = (await box.count()) > 0
  if (out.input) {
    const typed = Date.now()
    await box.click()
    await box.pressSequentially(keyword, { delay: 120 })
    const typedFor = Date.now() - typed
    await sleep(2500)
    await settle(8000)
    const hit = (since) => out.requests.some((r) => r.at >= since && r.url.toLowerCase().includes(keyword.toLowerCase()))
    const changed = async () => JSON.stringify(await links()) !== JSON.stringify(before)
    // Live: the list followed the typing, with no Enter and no button.
    out.live = hit(typed) || (await changed())
    // Debounced: fewer requests than keystrokes while typing.
    out.requests_while_typing = out.requests.filter((r) => r.at >= typed && r.at <= typed + typedFor + 2500).length
    if (!out.live) {
      await box.press('Enter')
      await sleep(1500)
      await settle(8000)
    }
    const after = await links()
    const text = await page.evaluate(() => document.body.innerText)
    out.stories_after = new Set(after).size
    out.keyword_shown = text.toLowerCase().includes(keyword.toLowerCase())
    out.filtered = out.stories_after > 0 && out.keyword_shown && (hit(typed) || JSON.stringify(after) !== JSON.stringify(before))
    // The keyword itself marked inside a title: an element holding just that word.
    out.keyword_marked = await page.evaluate((k) => [...document.querySelectorAll('mark, a span, a strong, a em, a b, h1 span, h2 span, h3 span, h3 strong')]
      .some((e) => e.textContent.trim().toLowerCase() === k.toLowerCase()), keyword)
    // A matching story set apart as a whole (the first build's reading of "highlight").
    out.card_marked = await page.evaluate(() => !!document.querySelector('[class*="highlight" i], [class*="match" i], mark'))
  }
  out.api_failures = out.requests.filter((r) => r.status >= 400).map((r) => `${r.status} ${r.url}`)
  out.requests = out.requests.map((r) => r.url).slice(0, 12)
} catch (e) {
  out.failed = String(e).slice(0, 400)
} finally {
  await browser.close()
}
console.log(JSON.stringify(out))
