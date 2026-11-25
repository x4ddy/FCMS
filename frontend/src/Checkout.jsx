import React, { useRef, useState } from 'react'
import { Avatar, Icon, money, ProductImage } from './ui.jsx'

const problems = {
  ADDRESS_NEEDS_PIN: 'This address needs a map pin.',
  OUTSIDE_DELIVERY_AREA: 'This address is outside the delivery area.',
  MAPS_UNAVAILABLE: 'Coverage is temporarily unavailable for this address.',
}
const distinctAddress = ({ kind, address_id }) => `${kind}:${address_id}`

export function DeliveryGroups({ groups, shortages = [], products = new Map() }) {
  return <div className="deliveries">
    {groups.map((group, index) => {
      const address = group.address
      const shortagesHere = shortages.filter((shortage) => shortage.group_number === index + 1)
      return <section className="panel delivery" key={distinctAddress(group.address_ref)}>
        <div className="delivery-head">
          <Icon name="pin" size={22} />
          <div>
            <h3>{address.label} · {address.recipient_name}</h3>
            <p className="muted">{[address.address_line1, address.address_line2, address.city,
              address.postal_code].filter(Boolean).join(', ')}</p>
            {group.store_name && <small className="muted">From {group.store_name}</small>}
          </div>
          <strong className="price">{money(group.subtotal_fils)}</strong>
        </div>
        {group.problem && <p className="notice error">{problems[group.problem]}</p>}
        <ul className="delivery-lines">
          {group.lines.map((line) => <li key={JSON.stringify([line.sku, line.contributor_id])}>
            <ProductImage product={products.get(line.sku) ?? line} />
            <span><strong>{line.quantity} × {line.name}</strong>
              <small className="muted">{[line.contributor_name || line.contributor_id, `${money(line.unit_price_fils)} each`]
                .filter(Boolean).join(' · ')}</small></span>
            <span className="price">{money(line.line_total_fils)}</span>
          </li>)}
        </ul>
        {shortagesHere.map(({ sku, requested, available }) => <p className="notice error" key={sku}>
          {group.lines.find((line) => line.sku === sku)?.name}: {available} available, {requested} requested
          across this store’s deliveries.
        </p>)}
      </section>
    })}
  </div>
}

export function OrderConfirmation({ order, error, products, onBack, onShop }) {
  return <section className="page narrow">
    <button className="text-button" onClick={onBack}>← All orders</button>
    {error ? <p role="alert" className="notice error">{error}</p> : !order ?
      <div className="panel skeleton tall" role="status" aria-label="Loading your order" /> : <>
        <div className="confirmation">
          <span className="confirmation-tick" aria-hidden="true"><Icon name="check" size={34} /></span>
          <div><h1>Order placed</h1><p className="muted">Order {order.order_number}</p></div>
          <div className="confirmation-total"><small className="muted">Total</small><strong>{money(order.total_fils)}</strong></div>
        </div>
        <h2>Delivery details</h2>
        <DeliveryGroups groups={order.delivery_groups} products={products} />
        <button onClick={onShop}>Continue shopping</button>
      </>}
  </section>
}

export function Checkout({
  account, cart, rows, changes, waiting, user, names, products, addresses, allowed, isAdult, editor, pending, onPlace, onOpenOrder,
}) {
  const [chosenAddress, setChosenAddress] = useState('')
  const [split, setSplit] = useState(false)
  const [overrides, setOverrides] = useState({})
  const [editing, setEditing] = useState(false)
  const [quote, setQuote] = useState(null)
  const [reviewError, setReviewError] = useState(null)
  const [decline, setDecline] = useState(false)
  const [busy, setBusy] = useState(false)
  const action = useRef(false)
  // The choice made in the cart. Live updates must not change what is being ordered.
  const [order, setOrder] = useState(() => rows)

  const addressIndex = new Map(addresses.map((address) => [distinctAddress(address), address]))
  const defaultAddress = addressIndex.get(chosenAddress) || addresses.find((address) => address.pinned) || addresses[0]
  const contributors = new Map()
  for (const row of order) for (const item of row.people) {
    contributors.set(item.contributor_id, [...contributors.get(item.contributor_id) ?? [], `${item.quantity} × ${row.product.name}`])
  }
  const total = order.reduce((sum, row) => sum + row.product.price_fils * row.quantity, 0)

  async function run(callback) {
    if (action.current) return
    action.current = true
    setBusy(true)
    setReviewError(null)
    try { await callback() }
    catch (error) {
      if (['QUOTE_STALE', 'QUOTE_EXPIRED', 'QUOTE_ALREADY_USED', 'QUOTE_NOT_FOUND', 'SELECTION_CHANGED',
        'OUT_OF_STOCK', 'CHECKOUT_FORBIDDEN', 'AGE_ELIGIBILITY_REQUIRED'].includes(error.code)) {
        setQuote(null)
        setOrder(rows)
      }
      if (error.code === 'MOCK_PAYMENT_DECLINED') setDecline(false)
      setReviewError(error)
    } finally {
      setBusy(false)
      action.current = false
    }
  }

  function clearReview() {
    setQuote(null)
    setReviewError(null)
    setOrder(rows)
  }

  function review() {
    return run(async () => {
      const reference = ({ kind, address_id }) => ({ kind, address_id })
      const submitted = !split ? [] : [...contributors.keys()].flatMap((id) => {
        const address = addressIndex.get(overrides[id])
        return address ? [{ contributor_id: id, address: reference(address) }] : []
      })
      setQuote(null)
      setQuote(await account.quote({
        cart, default_address: reference(defaultAddress), overrides: submitted, skus: order.map((row) => row.sku),
      }))
    })
  }

  const option = (address) => <option value={distinctAddress(address)} key={distinctAddress(address)}>
    {address.label} · {address.city}{address.owner ? ` (${address.owner})` : ''}{address.pinned ? '' : ' · needs pin'}
  </option>
  const message = reviewError && <div className="notice error" role="alert">
    {reviewError.status ? reviewError.message : 'Connection interrupted. Retry to check the result.'}
    {reviewError.detail?.order_id && <button className="secondary" onClick={() => onOpenOrder(reviewError.detail.order_id)}>
      Open existing confirmation</button>}
  </div>
  const failedGroups = reviewError?.detail?.delivery_groups

  if (pending) return <div className="drawer-body">
    {message}
    <section className="panel"><h2>Confirm your last submission</h2>
      <p className="muted">Retry to retrieve the result of the same order attempt. You will not be charged twice.</p>
      <button disabled={busy} onClick={() => run(() => onPlace(pending))}>{busy ? 'Checking…' : 'Retry submission'}</button>
    </section>
  </div>
  if (!allowed || !isAdult || order.length === 0) return <div className="drawer-body">{message}<p className="notice error">
    {!allowed ? 'You can no longer check out this cart.'
      : !isAdult ? 'Checkout is available to adults aged 18 and older.' : 'Nothing is selected any more. Go back to the cart and choose items.'}</p></div>

  return <>
    <div className="drawer-body">
      <ol className="steps" aria-label="Checkout progress">
        <li aria-current={quote ? undefined : 'step'}>1. Delivery</li><li aria-current={quote ? 'step' : undefined}>2. Review and pay</li>
      </ol>
      {message}
      {changes.length > 0 && <div className="notice" role="status"><strong>Your cart was just updated</strong>
        <span className="muted">{changes.join(' · ')}</span></div>}
      {waiting.length > 0 && <div className="notice" role="status"><strong>Waiting for the next cart</strong>
        <span className="muted">{waiting.map((row) => `${row.quantity} × ${row.product?.name ?? row.sku}`).join(' · ')}</span></div>}
      {failedGroups && <DeliveryGroups groups={failedGroups} shortages={reviewError.detail.shortages} products={products} />}
      {quote ? <>
        <DeliveryGroups groups={quote.delivery_groups} products={products} />
        <div className="panel bill">
          <div><span>Delivery</span><strong>{quote.delivery_fils ? money(quote.delivery_fils) : 'Free'}</strong></div>
          <div className="bill-total"><span>Total</span><strong>{money(quote.total_fils)}</strong></div>
          {quote.mock_outcome_enabled && <label className="checkbox-label"><input type="checkbox" checked={decline} disabled={busy}
            onChange={(event) => setDecline(event.target.checked)} /> Simulate a payment decline</label>}
        </div>
        <button className="text-button" disabled={busy} onClick={clearReview}>Change delivery</button>
      </> : editing ? <>
        <button className="text-button" onClick={() => setEditing(false)}>← Done with addresses</button>{editor}
      </> : <>
        <section className="panel">
          <h3>{order.length} {order.length === 1 ? 'product' : 'products'} selected</h3>
          <ul className="delivery-lines">{order.map((row) => <li key={row.sku}>
            <ProductImage product={row.product} />
            <span><strong>{row.quantity} × {row.product.name}</strong></span>
            <span className="price">{money(row.product.price_fils * row.quantity)}</span>
          </li>)}</ul>
        </section>
        <section className="panel form-stack">
          <div className="section-head"><h3>Deliver to</h3><button className="text-button" onClick={() => setEditing(true)}>Manage addresses</button></div>
          <div className="address-choices" role="radiogroup" aria-label="Delivery address">
            {addresses.map((address) => {
              const key = distinctAddress(address)
              return <label key={key} className="address-choice"><input type="radio" name="delivery" disabled={busy}
                checked={defaultAddress && distinctAddress(defaultAddress) === key}
                onChange={() => { setChosenAddress(key); clearReview() }} />
                <span><strong>{address.label}{address.owner ? ` · ${address.owner}` : ''}</strong>
                  <small className="muted">{address.address_line1}, {address.city}{address.pinned ? '' : ' · needs a map pin'}</small></span></label>
            })}
          </div>
          {contributors.size > 1 && <label className="checkbox-label"><input type="checkbox" checked={split} disabled={busy}
            onChange={(event) => { setSplit(event.target.checked); clearReview() }} /> Split delivery by who added the items</label>}
          {split && [...contributors].map(([id, items]) => {
            const name = id === user.id ? 'You' : names.get(id) ?? 'Member'
            return <label key={id} className="split-row">
              <span className="person"><Avatar id={id} name={name} />{name}<small className="muted">{items.join(', ')}</small></span>
              <select aria-label={`Delivery address for ${name}`} value={addressIndex.has(overrides[id]) ? overrides[id] : ''} disabled={busy}
                onChange={(event) => { setOverrides({ ...overrides, [id]: event.target.value }); clearReview() }}>
                <option value="">Same as above</option>{addresses.map(option)}
              </select></label>
          })}
        </section>
      </>}
    </div>
    {!editing && <footer className="drawer-footer">
      <div><strong>{money(quote?.total_fils ?? total)}</strong><small className="muted">Demo payment. No charge.</small></div>
      {quote ? <button className="big" disabled={busy} onClick={() => run(() => onPlace({
        key: crypto.randomUUID(), cart,
        body: { quote_id: quote.quote_id, ...(decline ? { mock_outcome: 'decline' } : {}) },
      }))}>{busy ? 'Placing order…' : 'Place order'}</button>
        : <button className="big" disabled={busy || !defaultAddress} onClick={review}>{busy ? 'Checking…' : 'Review order'}</button>}
    </footer>}
  </>
}
