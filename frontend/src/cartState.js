function contributionQuantities(contributions = []) {
  return new Map(contributions.map((item) => [
    `${item.contributor_id}\u0000${item.sku}`,
    item.quantity,
  ]))
}

/**
 * Who made one change. Live updates arrive in batches, so several people may
 * have edited the cart since the last snapshot (`actorIds`, in order).
 * - Adding always goes to the adder's own line, so the line's owner added it.
 * - A removal is the owner's own if they acted in this batch; otherwise someone
 *   else removed it (the primary, or checkout emptying the cart).
 */
function responsibleFor(contributorId, difference, actorId, actorIds) {
  if (!actorIds) return actorId ?? contributorId
  if (difference > 0 || actorIds.includes(contributorId)) return contributorId
  const others = actorIds.filter((id) => id !== contributorId)
  return others.at(-1) ?? actorId ?? contributorId
}

export function cartActivity(previous, current, currentUserId, actorId, memberNames, productNames, actorIds = null, orderedBy = null) {
  const before = contributionQuantities(previous)
  const after = contributionQuantities(current)
  const keys = new Set([...before.keys(), ...after.keys()])

  // Checkout removes what was ordered. That is one order, not a removal of every item.
  const order = orderedBy && orderedBy !== currentUserId
    ? [`${memberNames.get(orderedBy) ?? 'A household member'} placed an order`] : []
  const changes = [...keys].flatMap((key) => {
    const separator = key.indexOf('\u0000')
    const contributorId = key.slice(0, separator)
    const sku = key.slice(separator + 1)
    const difference = (after.get(key) ?? 0) - (before.get(key) ?? 0)
    const responsibleUserId = responsibleFor(contributorId, difference, actorId, actorIds)
    if (orderedBy && difference < 0) return []
    if (!difference || responsibleUserId === currentUserId) return []
    const person = memberNames.get(responsibleUserId) ?? 'A household member'
    const product = productNames.get(sku) ?? sku
    return [`${person} ${difference > 0 ? 'added' : 'removed'} ${Math.abs(difference)} × ${product}`]
  })
  return [...order, ...changes]
}

/** Cart contents plus what waits for the next cart, so moving between them is not an addition or a removal. */
export function combined(cart) {
  const rows = new Map()
  for (const row of [...cart.contributions, ...cart.waiting ?? []]) {
    const key = `${row.contributor_id}\u0000${row.sku}`
    rows.set(key, { ...row, quantity: row.quantity + (rows.get(key)?.quantity ?? 0) })
  }
  return [...rows.values()]
}

/** What changed between the cart on screen and the latest one, per product. */
export function cartDifferences(shown, latest, productNames) {
  const before = cartQuantities(shown)
  const after = cartQuantities(latest)
  return [...new Set([...Object.keys(before), ...Object.keys(after)])].flatMap((sku) => {
    const difference = (after[sku] ?? 0) - (before[sku] ?? 0)
    const name = productNames.get(sku) ?? sku
    if (!difference) return []
    return [difference > 0 ? `${difference} × ${name} added` : `${-difference} × ${name} removed`]
  })
}

/** Total quantity of each product across everyone in the cart. */
export function cartQuantities(contributions = []) {
  return contributions.reduce((totals, item) => ({
    ...totals, [item.sku]: (totals[item.sku] ?? 0) + item.quantity,
  }), {})
}

/** Quantities with taps that are not saved yet on top. */
export function withTaps(quantities, ...taps) {
  const next = { ...quantities }
  for (const [sku, delta] of taps.flatMap((group) => Object.entries(group))) {
    next[sku] = Math.max(0, (next[sku] ?? 0) + delta)
  }
  return next
}

/**
 * Apply a saved change the way the server does: adding goes to your own line,
 * removing takes your own units first and then other members'.
 */
export function applyAdjustment(contributions, sku, delta, userId) {
  const rows = contributions.map((row) => ({ ...row }))
  if (delta > 0) {
    const own = rows.find((row) => row.sku === sku && row.contributor_id === userId)
    if (own) own.quantity += delta
    else rows.push({ sku, contributor_id: userId, quantity: delta })
    return rows
  }
  let remaining = -delta
  const order = rows.filter((row) => row.sku === sku).sort((a, b) =>
    (b.contributor_id === userId) - (a.contributor_id === userId) || (a.contributor_id < b.contributor_id ? -1 : 1))
  for (const row of order) {
    const taken = Math.min(remaining, row.quantity)
    row.quantity -= taken
    remaining -= taken
  }
  return rows.filter((row) => row.quantity > 0)
}

/** One row per product in the cart, in catalogue order, with everyone who added it. */
export function cartRows(contributions, quantities, products) {
  const rows = products.filter((product) => quantities[product.sku] > 0)
    .map((product) => ({ sku: product.sku, product, quantity: quantities[product.sku] }))
  const known = new Set(products.map((product) => product.sku))
  for (const sku of Object.keys(quantities)) {
    if (!known.has(sku) && quantities[sku] > 0) rows.push({ sku, product: null, quantity: quantities[sku] })
  }
  return rows.map((row) => ({ ...row, people: contributions.filter((item) => item.sku === row.sku) }))
}
