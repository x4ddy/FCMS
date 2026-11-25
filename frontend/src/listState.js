// Pure helpers for shared shopping lists, kept free of React so they can be
// unit-tested with `node --test`.

const OTHER = 'Other items'

/** "3 eggs", "3x eggs" and "eggs" -> { quantity, text } */
export function parseQuickAdd(input) {
  const text = input.trim()
  const match = /^(\d{1,3})\s*[x×]?\s+(.+)$/i.exec(text)
  if (match && Number(match[1]) >= 1) return { quantity: Number(match[1]), text: match[2].trim() }
  return { quantity: 1, text }
}

/** Best catalogue matches for the quick-add box: prefix matches first, then word and substring matches. */
export function productSuggestions(products, query, limit = 6) {
  const search = query.trim().toLowerCase()
  if (search.length < 2) return []
  const scored = []
  for (const product of products) {
    const name = product.name.toLowerCase()
    const haystack = `${name} ${product.category.toLowerCase()}`
    let score
    if (name.startsWith(search)) score = 0
    else if (name.split(/[\s,]+/).some((word) => word.startsWith(search))) score = 1
    else if (haystack.includes(search)) score = 2
    else continue
    scored.push({ score, product })
  }
  return scored
    .sort((a, b) => a.score - b.score || a.product.name.localeCompare(b.product.name))
    .slice(0, limit)
    .map(({ product }) => product)
}

/** Split items into open items grouped by catalogue category, and done items (most recently ticked first). */
export function groupListItems(items, productsBySku, categoryOrder = []) {
  const open = new Map()
  const done = []
  for (const item of items) {
    if (item.checked) { done.push(item); continue }
    const category = productsBySku.get(item.sku)?.category ?? OTHER
    if (!open.has(category)) open.set(category, [])
    open.get(category).push(item)
  }
  const rank = (category) => {
    if (category === OTHER) return Number.MAX_SAFE_INTEGER
    const index = categoryOrder.indexOf(category)
    return index === -1 ? Number.MAX_SAFE_INTEGER - 1 : index
  }
  const groups = [...open.entries()].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
  return { groups, done }
}

/** Unticked catalogue items that can be sent to a cart, with an estimated total in fils. */
export function cartReadyItems(items, productsBySku) {
  const ready = items.filter((item) => !item.checked && item.sku && productsBySku.has(item.sku))
  const totalFils = ready.reduce(
    (total, item) => total + productsBySku.get(item.sku).price_fils * item.quantity, 0,
  )
  return { ready, totalFils }
}

export function itemLabel(item, productsBySku) {
  return item.custom_name ?? productsBySku.get(item.sku)?.name ?? item.sku
}

export function initials(name = '') {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}

const AVATAR_HUES = [145, 200, 28, 265, 340, 175, 50, 220]
export function avatarHue(id = '') {
  let hash = 0
  for (const character of id) hash = (hash * 31 + character.charCodeAt(0)) >>> 0
  return AVATAR_HUES[hash % AVATAR_HUES.length]
}

const relative = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
export function timeAgo(iso, now = Date.now()) {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000)
  const steps = [[60, 'second'], [60, 'minute'], [24, 'hour'], [7, 'day'], [4.35, 'week'], [12, 'month']]
  let value = seconds
  for (const [size, unit] of steps) {
    if (Math.abs(value) < size) return unit === 'second' ? 'just now' : relative.format(Math.round(value), unit)
    value /= size
  }
  return relative.format(Math.round(value), 'year')
}

/**
 * Apply a list from a live batch to the open list.
 * Batches arrive every ~20 s, so they can be older than what this screen already
 * knows: skip them while one of our own edits is still being saved (the next batch
 * will include it) or when they are older than the latest change we saw.
 */
export function mergeListUpdate(current, incoming, editsInFlight = 0) {
  if (!current || current.id !== incoming.id) return current
  if (editsInFlight > 0 || incoming.updated_at < current.updated_at) return current
  const pending = current.items.filter((item) => item.pending)
  return { ...incoming, items: [...incoming.items, ...pending] }
}

/** Keep the overview cards in step with a list detail or a live update, newest first. */
export function upsertListSummary(lists, entry) {
  if (entry.deleted) return lists.filter((list) => list.id !== entry.id)
  const existing = lists.find((list) => list.id === entry.id)
  if (existing && existing.updated_at > entry.updated_at) return lists
  const { items, ...summary } = entry
  const next = items ? {
    ...summary,
    item_count: items.filter((item) => !item.pending).length,
    checked_count: items.filter((item) => item.checked && !item.pending).length,
  } : summary
  return [next, ...lists.filter((list) => list.id !== entry.id)]
    .sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0))
}

/** Turn pasted text (one item per line, bullets allowed) into items for one bulk request. */
export function parsePastedList(text, products, limit = 50) {
  const byName = new Map(products.map((product) => [product.name.toLowerCase(), product]))
  return text.split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, '').trim())
    .filter(Boolean)
    .slice(0, limit)
    .map((line) => {
      const { quantity, text: name } = parseQuickAdd(line)
      const matches = productSuggestions(products, name, 2)
      const product = byName.get(name.toLowerCase()) ?? (matches.length === 1 ? matches[0] : null)
      return { quantity: Math.min(quantity, 999), text: name.slice(0, 120), product }
    })
}

/** The chosen items that can still be sent to a cart, and what they cost. */
export function chosenForCart(ready, chosenIds, productsBySku) {
  const items = ready.filter((item) => chosenIds.has(item.id) && !item.pending)
  const totalFils = items.reduce((total, item) => total + productsBySku.get(item.sku).price_fils * item.quantity, 0)
  return { items, totalFils }
}

/* Quick edits to a list wait a moment and are then sent together. `add` entries are
   { body, temporaryId }: temporaryId is null when the product is already on the list. */
export function emptyChanges(listId) {
  return { listId, add: [], set: new Map(), remove: new Set() }
}

export function hasChanges({ add, set, remove }) {
  return add.length > 0 || set.size > 0 || remove.size > 0
}

export function queueAdd(changes, items, body, temporaryId) {
  if (body.sku) {
    const waiting = changes.add.find((entry) => entry.body.sku === body.sku)
    if (waiting) {
      waiting.body.quantity = Math.min(999, waiting.body.quantity + body.quantity)
      return
    }
    const existing = items.find((item) => item.sku === body.sku && !item.checked && !changes.remove.has(item.id))
    const queued = existing && changes.set.get(existing.id)
    if (queued?.quantity != null) {
      queued.quantity = Math.min(999, queued.quantity + body.quantity)
      return
    }
    changes.add.push({ body: { ...body }, temporaryId: existing ? null : temporaryId })
    return
  }
  changes.add.push({ body: { ...body }, temporaryId })
}

/** Returns false if the item is still being created on the server and cannot be changed yet. */
export function queueSet(changes, item, fields) {
  if (item.pending) {
    const waiting = changes.add.find((entry) => entry.temporaryId === item.id)
    if (!waiting || fields.checked !== undefined) return false
    Object.assign(waiting.body, fields)
    return true
  }
  changes.set.set(item.id, { ...changes.set.get(item.id), ...fields })
  return true
}

export function queueRemove(changes, item) {
  if (item.pending) {
    const before = changes.add.length
    changes.add = changes.add.filter((entry) => entry.temporaryId !== item.id)
    return changes.add.length < before
  }
  changes.set.delete(item.id)
  changes.remove.add(item.id)
  return true
}

/** The list as it will be once the waiting changes are saved. */
export function applyChanges(items, changes, makeTemporary) {
  const next = items.filter((item) => !changes.remove.has(item.id) && !item.pending)
  for (const { body, temporaryId } of changes.add) {
    const index = temporaryId ? -1 : next.findIndex((item) => item.sku === body.sku && !item.checked)
    if (index >= 0) next[index] = { ...next[index], quantity: Math.min(999, next[index].quantity + body.quantity) }
    else next.push(makeTemporary(body, temporaryId))
  }
  return next.map((item) => changes.set.has(item.id) ? { ...item, ...changes.set.get(item.id) } : item)
}

export function changesBody({ add, set, remove }) {
  return {
    add: add.map(({ body }) => body), set: [...set].map(([id, fields]) => ({ id, ...fields })), remove: [...remove],
  }
}
