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

/** Quantities with changes that are not confirmed yet on top, in the order they were made. */
export function withOperations(quantities, operations) {
  const next = { ...quantities }
  for (const { kind, sku, delta } of operations) next[sku] = kind === 'remove' ? 0 : Math.max(0, (next[sku] ?? 0) + delta)
  return next
}

/** The cart with one confirmed change, unless what is shown is already newer and so contains it. */
export function applyConfirmed(cart, update) {
  if (!cart || cart.id !== update.cart_id || (cart.revision ?? 0) >= update.revision) return cart
  const part = update.waiting ? 'waiting' : 'contributions'
  return {
    ...cart, revision: update.revision,
    [part]: [...(cart[part] ?? []).filter((row) => row.sku !== update.sku), ...update.lines],
  }
}

/** One row per product in the cart, in catalogue order, with everyone who added it. Unresolved ones stay, even at 0. */
export function cartRows(contributions, quantities, products, unresolved = new Set()) {
  const listed = (sku) => quantities[sku] > 0 || unresolved.has(sku)
  const rows = products.filter((product) => listed(product.sku))
    .map((product) => ({ sku: product.sku, product, quantity: quantities[product.sku] ?? 0 }))
  const known = new Set(products.map((product) => product.sku))
  for (const sku of Object.keys(quantities)) {
    if (!known.has(sku) && quantities[sku] > 0) rows.push({ sku, product: null, quantity: quantities[sku] })
  }
  return rows.map((row) => ({ ...row, people: contributions.filter((item) => item.sku === row.sku) }))
}
