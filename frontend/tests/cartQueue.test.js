import assert from 'node:assert/strict'
import test from 'node:test'
import { createCartQueue, failureKind, newKey, retryDelay, SETTINGS, withTimeout } from '../src/cartQueue.js'
import { accountApi, householdApi, restoreSession, retryAfterSeconds } from '../src/api.js'
import { applyConfirmed, cartQuantities, withOperations } from '../src/cartState.js'
import { memoryStore } from '../src/operationStore.js'

const DAY = 24 * 60 * 60 * 1000
const MILK = { householdId: 'home', cartId: 'cart', sku: 'milk' }
const EGGS = { ...MILK, sku: 'eggs' }
const problem = (status, extra) => Object.assign(new Error(`status ${status}`), { status, ...extra })
const network = () => new TypeError('Failed to fetch')
const settle = async () => { for (let i = 0; i < 25; i += 1) await null }

/** A clock the test moves by hand. */
function fakeClock(start = 1_000_000_000) {
  let time = start
  let next = 0
  const timers = new Map()
  return {
    now: () => time,
    setTimer: (run, wait) => { timers.set(++next, { run, at: time + Math.max(0, wait) }); return next },
    clearTimer: (id) => { timers.delete(id) },
    async advance(milliseconds) {
      const end = time + milliseconds
      await settle()
      for (;;) {
        const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0]
        if (!due) break
        timers.delete(due[0])
        time = Math.max(time, due[1].at)
        due[1].run()
        await settle()
      }
      time = end
      await settle()
    },
  }
}

/** A server that applies each key once, like the real one, with failures on request. */
function fakeServer(clock) {
  const results = new Map()
  const server = {
    quantities: {}, requests: [], created: new Set(), active: 0, most: 0, latency: 20,
    // 'ok' | 'down' (503) | 'network' | 'late' (commits, the reply is lost) | 'hang' (never arrives) | function
    mode: 'ok', revision: 0,
    async send(operation) {
      server.requests.push({ id: operation.requestId, sku: operation.sku, delta: operation.delta, at: clock.now() })
      server.active += 1
      server.most = Math.max(server.most, server.active)
      try {
        const mode = typeof server.mode === 'function' ? server.mode(operation) : server.mode
        if (mode instanceof Error) { await server.wait(); throw mode }
        if (mode === 'network') { await server.wait(); throw network() }
        if (mode === 'down') { await server.wait(); throw problem(503) }
        if (mode !== 'hang') server.apply(operation)
        if (mode === 'hang' || mode === 'late') { await server.wait(SETTINGS.timeout); throw problem(0, { code: 'TIMEOUT' }) }
        await server.wait()
        return results.get(operation.requestId)
      } finally { server.active -= 1 }
    },
    wait: (milliseconds = server.latency) => new Promise((resolve) => { clock.setTimer(resolve, milliseconds) }),
    apply(operation) {
      if (results.has(operation.requestId)) return
      const { sku } = operation
      server.quantities[sku] = operation.kind === 'remove' ? 0 : Math.max(0, (server.quantities[sku] ?? 0) + operation.delta)
      server.revision += 1
      results.set(operation.requestId, {
        sku, cart_id: operation.cartId, quantity: server.quantities[sku], revision: server.revision, waiting: false,
        lines: server.quantities[sku] ? [{ sku, contributor_id: 'me', quantity: server.quantities[sku] }] : [],
      })
    },
    async createCart(operation) {
      server.created.add(operation.createKey)
      if (server.mode === 'network') throw network()
      return { id: 'new-cart' }
    },
  }
  return server
}

function browser({ clock = fakeClock(), server = fakeServer(clock), saved = new Map(), userId = 'me', leader = true, random = () => 0.5, ...rest } = {}) {
  const page = { clock, server, saved, failed: [], resolved: [], sent: [], isOnline: true, cart: { id: 'cart', revision: 0, contributions: [] } }
  page.queue = createCartQueue({
    userId, store: memoryStore(saved), send: server.send, createCart: server.createCart, random,
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer, online: () => page.isOnline,
    broadcast: (message) => page.sent.push(structuredClone(message)),
    onResolved: (operation, result) => { page.resolved.push(operation.requestId); page.cart = applyConfirmed(page.cart, result) },
    onFailed: (operation, error) => page.failed.push(error),
    ...rest,
  })
  page.shown = (sku = 'milk') => withOperations(cartQuantities(page.cart.contributions), page.queue.operations())[sku] ?? 0
  page.spinner = (sku = 'milk') => page.queue.operations().some((operation) => operation.sku === sku)
  page.tap = (delta = 1, where = MILK) => page.queue.adjust({ ...where, delta })
  page.ready = async () => { await page.queue.load(); await page.queue.setLeader(leader); return page }
  return page
}

test('a tap shows at once, is sent after the pause and settles', async () => {
  const page = await browser().ready()
  page.tap()
  assert.deepEqual([page.shown(), page.spinner()], [1, true])
  assert.equal(page.saved.size, 1)  // written down before anything is sent
  await page.clock.advance(399)
  assert.equal(page.server.requests.length, 0)
  await page.clock.advance(200)
  assert.deepEqual([page.shown(), page.spinner(), page.server.quantities.milk, page.saved.size], [1, false, 1, 0])
})

test('five rapid taps are one request; opposite taps cancel out', async () => {
  const page = await browser().ready()
  for (let i = 0; i < 5; i += 1) { page.tap(); await page.clock.advance(100) }
  await page.clock.advance(1000)
  assert.deepEqual(page.server.requests.map((request) => request.delta), [5])
  page.tap(1); page.tap(-1)
  await page.clock.advance(2000)
  assert.deepEqual([page.server.requests.length, page.saved.size, page.spinner()], [1, 0, false])
})

test('continuous taps send the first batch before the burst ends', async () => {
  const page = await browser().ready()
  const firstTapAt = page.clock.now()
  for (let i = 0; i < 10; i += 1) { page.tap(); await page.clock.advance(100) }
  assert.equal(page.shown(), 10)
  assert.deepEqual(page.server.requests.map(({ delta, at }) => [delta, at - firstTapAt]), [[8, SETTINGS.maxBatchWait]])
  await page.clock.advance(SETTINGS.debounce)
  assert.deepEqual(page.server.requests.map(({ delta }) => delta), [8, 2])
  assert.equal(page.server.quantities.milk, 10)
})

test('a tap while a request is on its way is a new change that waits its turn', async () => {
  const page = await browser().ready()
  page.server.latency = 3000
  page.tap(5)
  await page.clock.advance(500)
  page.tap(-2); page.tap(-1)
  await page.clock.advance(1000)
  assert.equal(page.server.requests.length, 1)  // not overtaken
  assert.equal(page.shown(), 2)
  await page.clock.advance(10000)
  assert.deepEqual(page.server.requests.map((request) => request.delta), [5, -3])
  assert.equal(new Set(page.server.requests.map((request) => request.id)).size, 2)
  assert.equal(page.server.quantities.milk, 2)
})

test('order is kept through failures, also with remove in between', async () => {
  const page = await browser().ready()
  page.server.mode = 'down'
  page.tap(3)
  await page.clock.advance(500)
  page.queue.remove(MILK)
  page.tap(2)
  await page.clock.advance(60000)
  assert.equal(new Set(page.server.requests.map((request) => request.id)).size, 1)  // only the first was ever tried
  assert.equal(page.shown(), 2)
  page.server.mode = 'ok'
  await page.clock.advance(60000)
  const firsts = [...new Map(page.server.requests.map((request) => [request.id, request.delta])).values()]
  assert.deepEqual(firsts, [3, undefined, 2])
  assert.deepEqual([page.server.quantities.milk, page.spinner()], [2, false])
})

test('different products do not wait for each other, up to the limit', async () => {
  const page = await browser().ready()
  page.server.latency = 1000
  for (const sku of ['a', 'b', 'c', 'd', 'e']) page.tap(1, { ...MILK, sku })
  await page.clock.advance(500)
  assert.equal(page.server.requests.length, 3)
  await page.clock.advance(5000)
  assert.deepEqual([page.server.requests.length, page.server.most], [5, 3])
})

test('a stuck product does not hold up another one', async () => {
  const page = await browser().ready()
  page.server.mode = (operation) => operation.sku === 'milk' ? 'hang' : 'ok'
  page.tap(); page.tap(1, EGGS)
  await page.clock.advance(1000)
  assert.deepEqual([page.spinner('milk'), page.spinner('eggs')], [true, false])
})

test('the database committed but the reply was lost: same key, counted once', async () => {
  const page = await browser().ready()
  page.server.quantities.milk = 5
  page.cart.contributions = [{ sku: 'milk', contributor_id: 'me', quantity: 5 }]
  let first = true
  page.server.mode = () => { const mode = first ? 'late' : 'ok'; first = false; return mode }
  page.tap()
  await page.clock.advance(5000)
  assert.deepEqual([page.shown(), page.spinner(), page.server.quantities.milk], [6, true, 6])
  await page.clock.advance(8000)
  assert.equal(page.server.requests.length, 2)
  assert.equal(new Set(page.server.requests.map((request) => request.id)).size, 1)
  assert.deepEqual([page.shown(), page.spinner(), page.server.quantities.milk], [6, false, 6])
})

test('a live update that already contains the change is not counted twice', async () => {
  const page = await browser().ready()
  page.server.mode = 'late'
  page.tap()
  await page.clock.advance(1000)
  const [operation] = page.queue.operations()
  const seen = []
  page.cart = { id: 'cart', revision: 1, contributions: [{ sku: 'milk', contributor_id: 'me', quantity: 1 }] }
  page.queue.markApplied([operation.requestId, 'somebody-elses'])
  seen.push(page.shown(), page.spinner(), page.saved.size)
  assert.deepEqual(seen, [1, false, 0])
  page.server.mode = 'ok'
  await page.clock.advance(60000)
  assert.deepEqual([page.shown(), page.server.quantities.milk, page.server.requests.length], [1, 1, 1])
})

test('a reply that is older than what is shown changes nothing', () => {
  const cart = { id: 'cart', revision: 9, contributions: [{ sku: 'milk', contributor_id: 'b', quantity: 4 }] }
  const update = { cart_id: 'cart', sku: 'milk', revision: 8, waiting: false, lines: [] }
  assert.equal(applyConfirmed(cart, update), cart)
  assert.equal(applyConfirmed(cart, { ...update, cart_id: 'other', revision: 10 }), cart)
  assert.deepEqual(applyConfirmed(cart, { ...update, revision: 10 }).contributions, [])
  const waiting = applyConfirmed(cart, { ...update, revision: 10, waiting: true, lines: [{ sku: 'milk', quantity: 1 }] })
  assert.deepEqual([waiting.contributions.length, waiting.waiting.length, waiting.revision], [1, 1, 10])
})

test('full jitter: between zero and the capped exponential delay', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 9].map((retry) => retryDelay(retry, () => 1)), [1000, 2000, 4000, 8000, 10000, 10000])
  assert.equal(retryDelay(3, () => 0), 0)
})

test('which errors are tried again', () => {
  const kinds = { 400: 'refused', 401: 'auth', 403: 'refused', 404: 'refused', 408: 'server', 409: 'refused', 422: 'refused', 429: 'busy', 500: 'server', 502: 'server', 503: 'server', 504: 'server', 200: 'network' }
  for (const [status, kind] of Object.entries(kinds)) assert.equal(failureKind(problem(Number(status))), kind, status)
  assert.equal(failureKind(network()), 'network')
  assert.equal(failureKind(problem(0, { code: 'TIMEOUT' })), 'server')
})

test('immediate retries end after 10 seconds; the change, the number and the spinner stay', async () => {
  const page = await browser({ random: () => 1 }).ready()
  page.server.mode = 'network'
  page.tap()
  await page.clock.advance(10500)
  const tries = page.server.requests.length
  assert.ok(tries >= 3 && tries <= 6, `${tries}`)
  assert.ok(page.server.requests.at(-1).at - page.server.requests[0].at <= 10000)
  for (let minute = 0; minute < 30; minute += 1) {
    await page.clock.advance(60000)
    assert.deepEqual([page.shown(), page.spinner(), page.saved.size, page.failed.length], [1, true, 1, 0])
  }
  assert.ok(page.server.requests.length < tries + 30 * 4 + 2)  // one careful try per cool-down
  page.server.mode = 'ok'
  await page.clock.advance(20000)
  assert.deepEqual([page.shown(), page.spinner(), page.server.quantities.milk, page.saved.size], [1, false, 1, 0])
  assert.equal(new Set(page.server.requests.map((request) => request.id)).size, 1)
})

test('never more than five immediate retries', async () => {
  const page = await browser({ random: () => 0 }).ready()
  page.server.mode = 'network'
  page.tap()
  await page.clock.advance(9000)
  assert.equal(page.server.requests.length, 6)
})

test('Retry-After is respected and does not extend the 10 second window', async () => {
  const page = await browser({ random: () => 0 }).ready()
  page.server.mode = problem(429, { retryAfter: 4 })
  page.tap()
  await page.clock.advance(60000 + 400)
  const times = page.server.requests.map((request) => request.at - page.server.requests[0].at)
  for (let i = 1; i < times.length; i += 1) assert.ok(times[i] - times[i - 1] >= 4000, `${times}`)
  assert.ok(times.filter((time) => time <= 10000).length <= 3, `${times}`)
  assert.equal(page.queue.state().breaker, 'closed')  // being asked to slow down is not a failure

  const slow = await browser().ready()
  slow.server.mode = problem(429, { retryAfter: 60 })
  slow.tap()
  await slow.clock.advance(59000)
  assert.equal(slow.server.requests.length, 1)  // later than the window: no immediate retry at all
  assert.equal(slow.spinner(), true)
  slow.server.mode = 'ok'
  await slow.clock.advance(5000)
  assert.equal(slow.spinner(), false)
})

test('circuit: opens after three server failures, one probe at a time, reopens, then closes and drains gently', async () => {
  const page = await browser({ random: () => 0 }).ready()
  page.server.mode = 'down'
  const skus = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
  for (const sku of skus) page.tap(1, { ...MILK, sku })
  await page.clock.advance(1000)
  assert.equal(page.queue.state().breaker, 'open')
  const opened = page.server.requests.length
  await page.clock.advance(13000)
  assert.equal(page.server.requests.length, opened)  // nothing is sent while it is open
  for (const sku of skus) assert.equal(page.spinner(sku), true)
  await page.clock.advance(2500)
  assert.equal(page.server.requests.length, opened + 1)  // the probe failed
  assert.equal(page.queue.state().breaker, 'open')
  await page.clock.advance(15100)
  assert.equal(page.server.requests.length, opened + 2)
  page.server.mode = 'ok'
  page.server.most = 0
  const start = page.server.requests.length
  await page.clock.advance(15100)
  assert.equal(page.queue.state().breaker, 'closed')
  await page.clock.advance(1000)
  const times = page.server.requests.slice(start).map((request) => request.at)
  for (let i = 1; i < 3; i += 1) assert.ok(times[i] - times[i - 1] >= SETTINGS.drainGap, `${times}`)
  assert.equal(times.length, skus.length)
  await page.clock.advance(5000)
  assert.ok(page.server.most <= SETTINGS.concurrency)
  for (const sku of skus) assert.deepEqual([page.spinner(sku), page.server.quantities[sku]], [false, 1])
})

test('circuit: half-open while the probe is on its way, and new taps do not slip past it', async () => {
  const page = await browser({ random: () => 0 }).ready()
  page.server.mode = 'down'
  for (const sku of ['a', 'b', 'c']) page.tap(1, { ...MILK, sku })
  await page.clock.advance(1000)
  page.server.latency = 3000
  await page.clock.advance(15000)
  assert.equal(page.queue.state().breaker, 'half-open')
  const sent = page.server.requests.length
  page.tap(1, EGGS)
  await page.clock.advance(1500)
  assert.equal(page.server.requests.length, sent)
  assert.equal(page.spinner('eggs'), true)
})

test('ordinary refusals and lost connections do not open the circuit', async () => {
  const page = await browser().ready()
  for (const status of [400, 403, 404, 409, 422]) {
    page.server.mode = problem(status)
    page.tap(1, { ...MILK, sku: `p${status}` })
    await page.clock.advance(1000)
  }
  assert.deepEqual([page.queue.state().breaker, page.failed.length], ['closed', 5])
  page.server.mode = 'network'
  for (const sku of ['a', 'b', 'c', 'd']) page.tap(1, { ...MILK, sku })
  await page.clock.advance(30000)
  assert.equal(page.queue.state().breaker, 'closed')
})

test('stock conflicts remove the optimistic addition and carry availability to other tabs', async () => {
  const page = await browser().ready()
  const detail = { store_id: 'uae-dubai', sku: 'milk', available: 0, requested: 1 }
  page.server.mode = problem(409, { code: 'OUT_OF_STOCK', detail })
  page.tap(1)
  assert.equal(page.shown(), 1)
  await page.clock.advance(30000)
  assert.equal(page.shown(), 0)
  assert.equal(page.server.requests.length, 1)
  assert.deepEqual(page.sent.find((message) => message.type === 'failed').error.detail, detail)
})

test('offline: nothing is sent until the browser is online again', async () => {
  const page = await browser().ready()
  page.isOnline = false
  page.tap(2)
  await page.clock.advance(120000)
  assert.deepEqual([page.server.requests.length, page.spinner(), page.shown()], [0, true, 2])
  page.isOnline = true
  page.queue.wake()
  await page.clock.advance(1000)
  assert.deepEqual([page.server.quantities.milk, page.spinner()], [2, false])
})

test('one change that always breaks does not hold up the others', async () => {
  const page = await browser({ random: () => 0 }).ready()
  page.server.mode = (operation) => operation.sku === 'milk' ? problem(500) : 'down'
  page.tap(); page.tap(1, EGGS); page.tap(1, { ...MILK, sku: 'bread' })
  await page.clock.advance(20000)
  page.server.mode = (operation) => operation.sku === 'milk' ? problem(500) : 'ok'
  await page.clock.advance(120000)
  assert.deepEqual([page.spinner('eggs'), page.spinner('bread'), page.spinner('milk')], [false, false, true])
})

test('a permanent refusal ends the spinner, is reported, and later changes go on', async () => {
  const page = await browser().ready()
  page.server.mode = problem(403)
  page.tap(-1)
  await page.clock.advance(500)
  page.server.mode = 'ok'
  page.tap(2)
  await page.clock.advance(60000)
  assert.deepEqual([page.failed.length, page.spinner(), page.saved.size, page.server.quantities.milk], [1, false, 0, 2])
})

test('after a refresh or restart the same change is sent with the same key', async () => {
  const first = await browser().ready()
  first.server.mode = 'late'
  first.tap(3)
  await first.clock.advance(1000)
  const [{ requestId }] = first.queue.operations()
  first.queue.stop()  // closed while waiting for the reply
  const again = await browser({ clock: first.clock, server: first.server, saved: first.saved }).ready()
  assert.deepEqual([again.spinner(), again.queue.operations()[0].requestId], [true, requestId])
  again.server.mode = 'ok'
  await again.clock.advance(20000)
  assert.deepEqual([again.server.quantities.milk, again.spinner(), again.saved.size], [3, false, 0])
  assert.deepEqual([...new Set(again.server.requests.map((request) => request.id))], [requestId])
})

test('taps cut off by closing the page are still sent next time', async () => {
  const first = await browser().ready()
  first.tap(4)
  await first.clock.advance(50)
  first.queue.stop()
  const again = await browser({ clock: first.clock, server: first.server, saved: first.saved }).ready()
  await again.clock.advance(SETTINGS.orphaned + 1000)
  assert.equal(again.server.quantities.milk, 4)
})

test('changes wait at most seven days, then are removed and reported', async () => {
  const first = await browser().ready()
  first.server.mode = 'network'
  first.tap()
  await first.clock.advance(20000)
  first.queue.stop()
  await first.clock.advance(6 * DAY)
  const sixth = await browser({ clock: first.clock, server: first.server, saved: first.saved }).ready()
  assert.deepEqual([sixth.spinner(), sixth.failed.length], [true, 0])
  sixth.queue.stop()
  await first.clock.advance(DAY)
  const last = await browser({ clock: first.clock, server: first.server, saved: first.saved }).ready()
  assert.deepEqual([last.spinner(), last.failed.length, last.saved.size], [false, 1, 0])
})

test('two tabs: one sends, both show it, and the other takes over', async () => {
  const clock = fakeClock()
  const server = fakeServer(clock)
  const saved = new Map()
  const tabs = []
  const join = (from) => (message) => { for (const tab of tabs) if (tab !== from()) tab.queue.receive(structuredClone(message)) }
  let a, b
  a = browser({ clock, server, saved, broadcast: join(() => a) })
  b = browser({ clock, server, saved, leader: false, broadcast: join(() => b) })
  tabs.push(a, b)
  await a.ready(); await b.ready()
  b.tap(2)
  assert.deepEqual([a.shown(), b.shown(), a.spinner()], [2, 2, true])
  await clock.advance(1000)
  assert.deepEqual([server.requests.length, server.quantities.milk], [1, 2])
  assert.deepEqual([a.shown(), b.shown(), a.spinner(), b.spinner()], [2, 2, false, false])
  server.mode = 'network'
  b.tap(1)
  await clock.advance(1000)
  a.queue.stop()
  tabs.shift()
  server.mode = 'ok'
  await b.queue.setLeader(true)
  await clock.advance(1000)
  assert.deepEqual([server.quantities.milk, b.spinner(), saved.size], [3, false, 0])
})

test('even if every tab sends, the change is applied once', async () => {
  const clock = fakeClock()
  const server = fakeServer(clock)
  const saved = new Map()
  const a = await browser({ clock, server, saved }).ready()
  server.mode = 'network'
  a.tap(2)
  await clock.advance(500)
  const b = await browser({ clock, server, saved }).ready()
  server.mode = 'ok'
  await clock.advance(20000)
  assert.ok(server.requests.length >= 2)
  assert.deepEqual([server.quantities.milk, a.spinner(), b.spinner()], [2, false, false])
})

test('switching households keeps and sends what is waiting for the other one', async () => {
  const page = await browser().ready()
  page.server.mode = 'network'
  page.tap(1)
  page.tap(1, { ...MILK, householdId: 'other', cartId: 'theirs' })
  await page.clock.advance(1000)
  assert.equal(page.queue.keys('home').length, 1)
  assert.equal(page.queue.keys('other').length, 1)
  page.server.mode = 'ok'
  await page.clock.advance(20000)
  assert.equal(page.server.quantities.milk, 2)
})

test('signed out: waits, keeps the change, and goes on when the same person signs in', async () => {
  const page = await browser().ready()
  page.server.mode = problem(401)
  page.tap()
  await page.clock.advance(120000)
  assert.deepEqual([page.server.requests.length, page.spinner(), page.saved.size, page.failed.length], [1, true, 1, 0])
  page.server.mode = 'ok'
  page.queue.signedIn()
  await page.clock.advance(1000)
  assert.deepEqual([page.server.quantities.milk, page.spinner()], [1, false])
})

test("another person on this device never sees or sends somebody's changes", async () => {
  const first = await browser().ready()
  first.server.mode = 'network'
  first.tap(2)
  await first.clock.advance(1000)
  await first.queue.seal()
  first.queue.stop()
  first.server.mode = 'ok'
  const sent = first.server.requests.length
  const other = await browser({ clock: first.clock, server: first.server, saved: first.saved, userId: 'somebody-else' }).ready()
  other.queue.receive({ type: 'put', operation: [...first.saved.values()][0] })
  await first.clock.advance(60000)
  assert.deepEqual([other.queue.operations().length, first.server.requests.length, first.saved.size], [0, sent, 1])
  const back = await browser({ clock: first.clock, server: first.server, saved: first.saved }).ready()
  await first.clock.advance(1000)
  assert.deepEqual([back.server.quantities.milk, back.saved.size], [2, 0])
})

test('flush: true once everything is saved, false when time runs out', async () => {
  const page = await browser().ready()
  page.tap(2)
  let answer
  page.queue.flush(() => true, 10000).then((value) => { answer = value })
  await page.clock.advance(100)
  assert.equal(answer, true)  // without waiting for the debounce
  page.server.mode = 'hang'
  page.tap()
  answer = undefined
  page.queue.flush(() => true, 10000).then((value) => { answer = value })
  await page.clock.advance(9900)
  assert.equal(answer, undefined)
  await page.clock.advance(200)
  assert.deepEqual([answer, page.spinner()], [false, true])
  assert.equal(await page.queue.flush((item) => item.sku === 'eggs', 10), true)  // other carts do not matter
})

test('the first taps in a household without a cart create one cart', async () => {
  const carts = []
  const page = await browser({ onCart: (operation, cart) => carts.push(cart.id) }).ready()
  page.server.mode = 'network'
  page.tap(1, { ...MILK, cartId: null }); page.tap(1, { ...EGGS, cartId: null })
  await page.clock.advance(1000)
  page.server.mode = 'ok'
  await page.clock.advance(20000)
  assert.equal(page.server.created.size, 1)  // one key, so the server makes one cart
  assert.deepEqual([page.server.quantities.milk, page.server.quantities.eggs], [1, 1])
  assert.ok(carts.every((id) => id === 'new-cart'))
})

test('it still works when nothing can be stored', async () => {
  const clock = fakeClock()
  const page = await browser({ clock, store: { load: async () => { throw new Error('blocked') }, put: () => Promise.reject(new Error('full')), remove: () => Promise.reject(new Error('full')) } }).ready()
  page.tap()
  await clock.advance(1000)
  assert.deepEqual([page.server.quantities.milk, page.spinner()], [1, false])
})

test('deleting a cart drops what was waiting for it', async () => {
  const page = await browser().ready()
  page.server.mode = 'network'
  page.tap()
  await page.clock.advance(1000)
  page.queue.discard((item) => item.cartId === 'cart')
  await page.clock.advance(60000)
  assert.deepEqual([page.spinner(), page.saved.size, page.failed.length], [false, 0, 0])
})

test('a request that takes too long is given up and cancelled', async () => {
  let signal
  await assert.rejects(withTimeout((given) => { signal = given; return new Promise(() => {}) }, 20), (error) => failureKind(error) === 'server')
  assert.equal(signal.aborted, true)
  assert.equal(await withTimeout(async () => 'answer', 1000), 'answer')
  assert.notEqual(newKey(), newKey())
})

test('Retry-After is read from headers as seconds or a date', async () => {
  assert.equal(retryAfterSeconds('7'), 7)
  assert.equal(retryAfterSeconds(new Date(60_000).toUTCString(), 0), 60)
  assert.equal(retryAfterSeconds(null), 0)
  globalThis.fetch = async () => new Response('{"detail":"busy"}', { status: 503, headers: { 'Retry-After': '3' } })
  await assert.rejects(householdApi('t', 'home').adjustSharedQuantity('c', 'milk', 1, {}), (error) => error.retryAfter === 3)
})

test("one person's change is refused under another person's token, also after a token refresh", async () => {
  const token = (sub) => `x.${btoa(JSON.stringify({ sub }))}.y`
  const sent = []
  globalThis.fetch = async (path, options) => {
    sent.push([path, options.headers?.Authorization])
    if (path.endsWith('/sessions/refresh')) return new Response(JSON.stringify({ access_token: token('bilal'), user: { id: 'bilal' } }))
    return new Response('{"detail":"expired"}', { status: 401 })
  }
  accountApi(token('bilal'))
  await assert.rejects(householdApi(token('bilal'), 'home').adjustSharedQuantity('c', 'milk', 1, { owner: 'asha' }), (error) => error.status === 401)
  assert.equal(sent.length, 0)
  await assert.rejects(householdApi(token('asha'), 'home').adjustSharedQuantity('c', 'milk', 1, { owner: 'asha' }), (error) => error.status === 401)
  assert.deepEqual(sent.map(([path]) => path.split('/').at(-1)), ['milk', 'refresh'])  // never sent as Bilal
  await restoreSession()
})
