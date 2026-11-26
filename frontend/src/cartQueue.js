// Sends cart changes to the server until each one is confirmed or refused for good.
// Kept free of React and the browser so it can be tested with `node --test`.

const DAY = 24 * 60 * 60 * 1000

export const SETTINGS = {
  debounce: 400,
  maxBatchWait: 750,     // from the first tap, even if taps keep coming
  timeout: 5000,        // per request
  baseDelay: 1000,
  multiplier: 2,
  maxDelay: 10000,
  window: 10000,        // how long a change is retried straight away; afterwards it waits for recovery
  maxRetries: 5,        // within that window, not counting the first request
  failuresToOpen: 3,    // server failures in a row
  cooldown: 15000,
  cooldownJitter: 3000,
  concurrency: 3,
  drainGap: 150,
  brokenPause: 30000,   // before a change that gets a 500 may be tried again
  orphaned: 5000,       // a change left open by a tab that went away
  maxAge: 7 * DAY,
}

/** Full jitter: anywhere between 0 and the capped exponential delay. */
export function retryDelay(retry, random = Math.random, settings = SETTINGS) {
  return random() * Math.min(settings.maxDelay, settings.baseDelay * settings.multiplier ** retry)
}

/**
 * What a failed request means.
 * - network: nothing is known about the server. Never counts against it.
 * - server: it was reached, or should have been, and had a problem.
 * - busy: it asked us to slow down.
 * - auth: wait for this person to sign in again.
 * - refused: trying again cannot help.
 */
export function failureKind(error) {
  const status = error?.status
  if (status === 401) return 'auth'
  if (status === 429) return 'busy'
  if (status === 408 || status >= 500 || error?.code === 'TIMEOUT') return 'server'
  if (!status || status < 400) return 'network'  // no answer, or one that could not be read
  return 'refused'
}

export function newKey() {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID()
  return [...globalThis.crypto.getRandomValues(new Uint8Array(16))].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function timeoutError() {
  return Object.assign(new Error('The server took too long to answer.'), { code: 'TIMEOUT', status: 0 })
}

/** Stop waiting after a while. The request may still have been processed. */
export function withTimeout(start, milliseconds) {
  const controller = new AbortController()
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { reject(timeoutError()); controller.abort() }, milliseconds)
    Promise.resolve().then(() => start(controller.signal)).then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

/**
 * `send(operation)` and `createCart(operation)` make one request each and reject with its error.
 * Events: onChange(operations), onResolved(operation, result), onFailed(operation, error), onCart(operation, cart).
 */
export function createCartQueue({
  userId, store, send, createCart, onChange = () => {}, onResolved = () => {}, onFailed = () => {}, onCart = () => {},
  broadcast = () => {}, online = () => true, random = Math.random, now = Date.now, settings: overrides,
  setTimer = (run, wait) => setTimeout(run, wait), clearTimer = (timer) => clearTimeout(timer),
}) {
  const settings = { ...SETTINGS, ...overrides }
  let operations = []             // oldest first
  const own = new Set()           // made in this tab
  const inFlight = new Set()
  const settled = new Set()       // so a late message from another tab cannot bring one back
  const schedule = new Map()      // requestId -> { retries, windowStart, nextAt, parked, notBefore }
  const waiters = new Set()
  let leader = false
  let stopped = false
  let signedOut = false
  let breaker = 'closed'
  let failures = 0
  let recoverAt = 0
  let lastProbe = -Infinity
  let probing = null
  let lastProbed = null
  let limit = settings.concurrency
  let gapUntil = 0
  let timer = null
  let lastOrder = 0

  const lane = (operation) => `${operation.householdId}\u0000${operation.sku}`
  const planned = (operation) => {
    if (!schedule.has(operation.requestId)) {
      schedule.set(operation.requestId, { retries: 0, windowStart: null, nextAt: 0, parked: false, notBefore: 0 })
    }
    return schedule.get(operation.requestId)
  }

  function changed() {
    operations = [...operations]
    onChange(operations)
    for (const waiter of [...waiters]) waiter()
  }

  async function save(operation) {
    try { await store.put(operation) } catch { /* it stays in this page's memory */ }
  }

  function publish(operation) {
    broadcast({ type: 'put', operation })
    return save(operation)
  }

  function take(id) {
    const operation = operations.find((item) => item.requestId === id)
    if (!operation) return null
    operations = operations.filter((item) => item !== operation)
    settled.add(id)
    schedule.delete(id)
    own.delete(id)
    return operation
  }

  function forget(id) {
    store.remove(id).catch(() => {})
  }

  function insert(operation) {
    operations.push(operation)
    operations.sort((a, b) => a.order - b.order || (a.requestId < b.requestId ? -1 : 1))
  }

  function add(fields) {
    lastOrder = Math.max(lastOrder + 1, now() * 1000)
    const operation = {
      requestId: newKey(), userId, createdAt: now(), order: lastOrder, attempts: 0, ...fields,
    }
    if (!operation.cartId) {
      // Everything meant for a cart that does not exist yet shares one key, so one cart is created.
      operation.createKey = operations.find((item) => !item.cartId && item.householdId === operation.householdId)?.createKey ?? newKey()
    }
    own.add(operation.requestId)
    insert(operation)
    publish(operation)
    changed()
    pump()
  }

  /** Taps on one product within the debounce time become one change. Once it closes it is never altered. */
  function adjust({ householdId, cartId, sku, delta }) {
    const tappedAt = now()
    const last = operations.findLast((item) => item.householdId === householdId && item.sku === sku)
    const open = last && own.has(last.requestId) && last.kind === 'adjust' && !last.sealed && tappedAt < last.readyAt
      && last.cartId === cartId && Math.abs(last.delta + delta) <= 99
    if (!open) {
      const readyAt = tappedAt + Math.min(settings.debounce, settings.maxBatchWait)
      add({ kind: 'adjust', householdId, cartId, sku, delta, sealed: false, readyAt })
      return
    }
    last.delta += delta
    last.readyAt = Math.min(tappedAt + settings.debounce, last.createdAt + settings.maxBatchWait)
    if (last.delta === 0) {
      // Tapped back to where it was: nothing to save, and nothing was sent.
      take(last.requestId)
      forget(last.requestId)
      broadcast({ type: 'gone', id: last.requestId })
    } else publish(last)
    changed()
    pump()
  }

  function remove({ householdId, cartId, sku }) {
    seal()
    add({ kind: 'remove', householdId, cartId, sku, sealed: true, readyAt: now() })
  }

  /** Close everything still collecting taps. Resolves when it is written down. */
  function seal() {
    const open = operations.filter((item) => own.has(item.requestId) && !item.sealed)
    for (const operation of open) operation.sealed = true
    const saved = Promise.all(open.map(publish))
    if (open.length > 0) pump()
    return saved
  }

  const ready = (operation) => operation.sealed || now() >= operation.readyAt + settings.orphaned

  /** The oldest unresolved change of each product. Later ones wait for it. */
  function heads() {
    const first = new Map()
    for (const operation of operations) if (!first.has(lane(operation))) first.set(lane(operation), operation)
    return [...first.values()].filter((operation) => !inFlight.has(operation.requestId) && ready(operation))
  }

  function nextProbe() {
    const able = heads().filter((operation) => planned(operation).notBefore <= now())
    // Not always the same one: a single broken change must not keep everything waiting.
    const index = able.findIndex((operation) => operation.requestId === lastProbed)
    return able[(index + 1) % able.length]
  }

  function coolDown() {
    recoverAt = now() + settings.cooldown + random() * settings.cooldownJitter
  }

  function pump() {
    if (stopped) return
    clearTimer(timer)
    for (const operation of operations) {
      if (own.has(operation.requestId) && !operation.sealed && now() >= operation.readyAt) {
        operation.sealed = true
        publish(operation)
      }
    }
    for (const operation of [...operations]) {
      if (now() - operation.createdAt > settings.maxAge && !inFlight.has(operation.requestId)) {
        fail(operation, new Error('A cart change from more than 7 days ago could not be saved, so it was removed.'))
      }
    }
    if (leader && !signedOut && online()) {
      if (breaker === 'closed' && !probing) {
        for (const operation of heads()) {
          if (inFlight.size >= limit || now() < gapUntil) break
          const plan = planned(operation)
          if (!plan.parked && plan.nextAt <= now()) attempt(operation)
        }
      }
      const waiting = breaker === 'open' || heads().some((operation) => planned(operation).parked)
      if (waiting && !probing && inFlight.size === 0 && now() >= recoverAt) {
        const operation = nextProbe()
        if (operation) {
          if (breaker === 'open') breaker = 'half-open'
          probing = lastProbed = operation.requestId
          lastProbe = now()
          attempt(operation)
        }
      }
    }
    planTimer()
  }

  function planTimer() {
    const times = []
    for (const operation of operations) {
      if (inFlight.has(operation.requestId)) continue
      const plan = planned(operation)
      if (!operation.sealed) times.push(own.has(operation.requestId) ? operation.readyAt : operation.readyAt + settings.orphaned)
      else if (!leader || signedOut || !online()) continue
      else if (plan.parked || breaker !== 'closed') times.push(Math.max(recoverAt, plan.notBefore))
      else times.push(Math.max(plan.nextAt, gapUntil))
    }
    // A change that is ready now is started by whatever it waits for, so only the future needs a timer.
    const next = Math.min(...times.filter((time) => time > now()))
    clearTimer(timer)
    if (Number.isFinite(next)) timer = setTimer(pump, next - now())
  }

  async function attempt(operation) {
    const id = operation.requestId
    const plan = planned(operation)
    inFlight.add(id)
    plan.windowStart ??= now()
    operation.attempts += 1
    operation.sealed = true
    let result
    let error = null
    try {
      // Written down before anything depends on the network.
      await Promise.race([save(operation), new Promise((resolve) => { setTimer(resolve, 500) })])
      if (stopped) { inFlight.delete(id); return }
      if (!operation.cartId) {
        const cart = await createCart(operation)
        assignCart(operation, cart)
        broadcast({ type: 'cart', operation, cart })
      }
      result = await send(operation)
    } catch (caught) { error = caught }
    inFlight.delete(id)
    const probe = probing === id
    if (probe) probing = null
    if (!error) {
      forget(id)
      broadcast({ type: 'resolved', id, operation, result })
      if (stopped) return
      answered()
      if (take(id)) {
        // Both in one go: the confirmed contents and the change that is no longer waiting.
        onResolved(operation, result)
        changed()
      }
    } else if (stopped) return
    else if (!operations.includes(operation)) {
      if (probe && breaker === 'half-open') breaker = 'open'
    } else handle(operation, plan, error, probe)
    pump()
  }

  function assignCart(operation, cart) {
    for (const item of operations) {
      if (item.createKey && item.createKey === operation.createKey) {
        item.cartId = cart.id
        save(item)
      }
    }
    operation.cartId = cart.id
    if (!stopped) {
      onCart(operation, cart)
      changed()
    }
  }

  /** The server answered properly, so waiting changes may go again, a few at a time. */
  function answered() {
    failures = 0
    const recovering = breaker !== 'closed' || [...schedule.values()].some((plan) => plan.parked)
    breaker = 'closed'
    if (recovering) {
      limit = 1
      for (const plan of schedule.values()) {
        if (plan.notBefore > now()) continue
        Object.assign(plan, { retries: 0, windowStart: null, nextAt: 0, parked: false })
      }
    } else if (limit < settings.concurrency) limit += 1
    gapUntil = limit < settings.concurrency ? now() + settings.drainGap : 0
  }

  function handle(operation, plan, error, probe) {
    const kind = failureKind(error)
    if (kind === 'refused') {
      answered()
      fail(operation, error)
      return
    }
    if (kind === 'auth') {
      signedOut = true
      if (breaker === 'half-open') breaker = 'open'
      return
    }
    if (kind === 'server') failures += 1
    if (breaker === 'half-open' || (kind === 'server' && failures >= settings.failuresToOpen)) {
      breaker = 'open'
      coolDown()
    }
    const asked = (error.retryAfter ?? 0) * 1000
    if (asked > 0) plan.notBefore = now() + asked
    const delay = Math.max(retryDelay(plan.retries, random, settings), asked)
    const tooLate = now() - plan.windowStart + delay > settings.window
    if (probe || plan.parked || plan.retries >= settings.maxRetries || tooLate) {
      // Out of immediate retries. It stays stored, and stays visible as unresolved.
      if (error.status === 500) plan.notBefore = Math.max(plan.notBefore, now() + settings.brokenPause)
      if (!plan.parked || probe) coolDown()
      plan.parked = true
      return
    }
    plan.retries += 1
    plan.nextAt = now() + delay
  }

  function fail(operation, error) {
    if (!take(operation.requestId)) return
    forget(operation.requestId)
    broadcast({ type: 'failed', id: operation.requestId, operation, error: {
      message: error.message, status: error.status, code: error.code, detail: error.detail,
    } })
    onFailed(operation, error)
    changed()
  }

  /** The server's data already contains these. Call together with showing that data. */
  function markApplied(keys) {
    let any = false
    for (const key of keys) {
      if (!take(key)) continue
      forget(key)
      any = true
    }
    if (any) { changed(); pump() }
  }

  function receive(message) {
    if (stopped) return
    if (message.type === 'put') {
      const { operation } = message
      if (operation.userId !== userId || settled.has(operation.requestId)) return
      const known = operations.find((item) => item.requestId === operation.requestId)
      if (known) Object.assign(known, operation, { attempts: Math.max(known.attempts, operation.attempts), cartId: known.cartId ?? operation.cartId })
      else insert(operation)
      changed()
    } else if (message.type === 'resolved') {
      if (take(message.id)) { onResolved(message.operation, message.result); changed() }
      settled.add(message.id)
    } else if (message.type === 'failed') {
      if (take(message.id)) { onFailed(message.operation, message.error); changed() }
      settled.add(message.id)
    } else if (message.type === 'gone') {
      if (take(message.id)) changed()
      settled.add(message.id)
    } else if (message.type === 'cart') assignCart({ ...message.operation }, message.cart)
    else if (message.type === 'wake') { wake(); return }
    pump()
  }

  async function load() {
    let saved = []
    try { saved = await store.load(userId) } catch { /* nothing readable */ }
    for (const operation of saved) {
      if (!settled.has(operation.requestId) && !operations.some((item) => item.requestId === operation.requestId)) insert(operation)
    }
    changed()
  }

  async function setLeader(value) {
    leader = value
    if (value) await load()  // what another tab left behind
    pump()
  }

  /** Something suggests the server can be reached again. Still one careful try at a time. */
  function wake() {
    if (now() - lastProbe >= 2000) recoverAt = Math.min(recoverAt, now())
    pump()
  }

  function matching(filter) {
    return operations.filter(filter)
  }

  /** Send what matches now. Resolves to whether everything was resolved in time. */
  function flush(filter, milliseconds) {
    seal()
    broadcast({ type: 'wake' })
    wake()
    return new Promise((resolve) => {
      const done = (value) => { waiters.delete(check); clearTimer(limitTimer); resolve(value) }
      const check = () => { if (matching(filter).length === 0) done(true) }
      const limitTimer = setTimer(() => done(matching(filter).length === 0), milliseconds)
      waiters.add(check)
      check()
    })
  }

  return {
    userId, adjust, remove, seal, flush, load, setLeader, markApplied, receive, wake,
    operations: () => operations,
    keys: (householdId) => operations.filter((item) => item.householdId === householdId).slice(0, 100).map((item) => item.requestId),
    state: () => ({ breaker, limit, inFlight: inFlight.size, signedOut }),
    signedIn() { signedOut = false; pump() },
    discard(filter) {
      for (const operation of matching(filter)) {
        take(operation.requestId)
        forget(operation.requestId)
        broadcast({ type: 'gone', id: operation.requestId })
      }
      changed()
    },
    stop() {
      stopped = true
      clearTimer(timer)
      for (const waiter of [...waiters]) waiter()
    },
  }
}

/** Connects the queue to the browser: storage, other tabs, and signs that the network is back. */
export async function openCartQueue(options, openStore) {
  const channel = globalThis.BroadcastChannel ? new BroadcastChannel(`familycart:cart:${options.userId}`) : null
  const queue = createCartQueue({
    ...options, store: await openStore(), online: () => navigator.onLine !== false,
    broadcast: (message) => channel?.postMessage(structuredClone(message)),
  })
  if (channel) channel.onmessage = ({ data }) => queue.receive(data)
  await queue.load()
  // One tab sends. Without this lock every tab would, which the server's keys keep harmless.
  let release = () => {}
  const held = new Promise((resolve) => { release = resolve })
  const waiting = new AbortController()
  if (navigator.locks) {
    navigator.locks.request(`familycart:cart:${options.userId}`, { signal: waiting.signal }, () => {
      queue.setLeader(true)
      return held
    }).catch(() => {})
  } else queue.setLeader(true)
  const wake = () => queue.wake()
  const visible = () => { if (document.visibilityState === 'visible') queue.wake() }
  const hide = () => { queue.seal() }
  window.addEventListener('online', wake)
  window.addEventListener('pagehide', hide)
  document.addEventListener('visibilitychange', visible)
  return {
    ...queue,
    stop() {
      queue.stop()
      waiting.abort()
      release()
      channel?.close()
      window.removeEventListener('online', wake)
      window.removeEventListener('pagehide', hide)
      document.removeEventListener('visibilitychange', visible)
    },
  }
}
