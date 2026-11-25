import React, { useRef, useState } from 'react'

const money = new Intl.NumberFormat('en-AE', { style: 'currency', currency: 'AED' })
const amount = (fils) => money.format(fils / 100)
const problems = {
  ADDRESS_NEEDS_PIN: 'This address needs a map pin.',
  OUTSIDE_DELIVERY_AREA: 'This address is outside the delivery area.',
  MAPS_UNAVAILABLE: 'Coverage is temporarily unavailable for this address.',
}
const distinctAddress = ({ kind, address_id }) => `${kind}:${address_id}`

export function DeliveryGroups({ groups, shortages = [] }) {
  return <div className="checkout-deliveries">
    {groups.map((group, index) => {
      const address = group.address
      const shortagesHere = shortages.filter((shortage) => shortage.group_number === index + 1)
      return <section className="panel checkout-group" key={distinctAddress(group.address_ref)}>
        <div className="checkout-group-heading">
          <div>
            <h3>{group.store_name || address.label}</h3>
            <strong>{address.recipient_name}</strong>
            <p className="muted">{[address.address_line1, address.address_line2, address.city,
              address.postal_code].filter(Boolean).join(', ')}</p>
          </div>
          <strong>{amount(group.subtotal_fils)}</strong>
        </div>
        {group.problem && <p className="notice error">{problems[group.problem]}</p>}
        <ul className="rows checkout-lines">
          {group.lines.map((line) => <li key={JSON.stringify([line.sku, line.contributor_id])}>
            <div>
              <strong>{line.quantity} × {line.name}</strong>
              <small>{[line.contributor_name || line.contributor_id, `${amount(line.unit_price_fils)} each`]
                .filter(Boolean).join(' · ')}</small>
            </div>
            <strong>{amount(line.line_total_fils)}</strong>
          </li>)}
        </ul>
        {shortagesHere.map(({ sku, requested, available }) => <p className="notice error" key={sku}>
          {group.lines.find((line) => line.sku === sku)?.name}
          {' — '}{available} available and {requested} requested across this store's deliveries.
        </p>)}
      </section>
    })}
  </div>
}

export function OrderConfirmation({ order, error, onBack }) {
  return <section className="checkout-page">
    <button className="text-button" onClick={onBack}>Back to order history</button>
    {error ? <p role="alert" className="notice error">{error}</p> : !order ?
      <p role="status">Loading your order…</p> : <>
        <div className="checkout-confirmation">
          <div><h1>Order placed</h1><p>Order <strong>{order.order_number}</strong></p></div>
          <div><span>Total</span><strong>{amount(order.total_fils)}</strong></div>
        </div>
        <h2>Delivery details</h2>
        <DeliveryGroups groups={order.delivery_groups} />
      </>}
  </section>
}

export function Checkout({
  account, cart, contributions, members, products, addresses, allowed, isAdult, editors,
  pending, onPlace, onOpenOrder, onBack,
}) {
  const [selected, setSelected] = useState('')
  const [overrides, setOverrides] = useState({})
  const [editor, setEditor] = useState('')
  const [quote, setQuote] = useState(null)
  const [reviewError, setReviewError] = useState(null)
  const [decline, setDecline] = useState(false)
  const [busy, setBusy] = useState(false)
  const action = useRef(false)

  const addressIndex = new Map(addresses.map((address) => [distinctAddress(address), address]))
  const defaultAddress = addressIndex.get(selected) || addresses.find((address) => address.pinned) || addresses[0]
  const names = new Map(members.map((member) => [member.id, member.display_name]))
  const productNames = new Map(products.map((product) => [product.sku, product.name]))
  const contributors = new Map()
  for (const item of contributions) {
    const rows = contributors.get(item.contributor_id) || []
    rows.push(`${item.quantity} × ${productNames.get(item.sku) || item.sku}`)
    contributors.set(item.contributor_id, rows)
  }

  async function run(callback) {
    if (action.current) return
    action.current = true
    setBusy(true)
    setReviewError(null)
    try { await callback() }
    catch (error) {
      if (['QUOTE_STALE', 'QUOTE_EXPIRED', 'QUOTE_ALREADY_USED', 'QUOTE_NOT_FOUND',
        'OUT_OF_STOCK', 'CHECKOUT_FORBIDDEN', 'AGE_ELIGIBILITY_REQUIRED'].includes(error.code)) setQuote(null)
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
  }

  function option(address) {
    const key = distinctAddress(address)
    return <option value={key} key={key}>
      {address.label} · {address.city} {address.owner ? `(${address.owner})` : ''}
      {address.pinned ? '' : ' · needs pin'}
    </option>
  }

  const errorMessage = reviewError && <p className="notice error" role="alert">
    {reviewError.status ? reviewError.message : 'Connection interrupted. Retry to check the result.'}
  </p>
  const failedGroups = reviewError?.detail?.delivery_groups

  return <section className="checkout-page">
    <button className="text-button" disabled={busy} onClick={onBack}>{pending ? 'Back' : 'Back to cart'}</button>
    <h1>Checkout</h1>
    {errorMessage}
    {reviewError?.detail?.order_id && <button className="secondary" onClick={() => onOpenOrder(reviewError.detail.order_id)}>
      Open existing confirmation
    </button>}
    {pending ? <section className="panel checkout-form">
      <h2>Confirm your last submission</h2>
      <p className="muted">Retry to retrieve the result of the same order attempt.</p>
      <button disabled={busy} onClick={() => run(() => onPlace(pending))}>{busy ? 'Checking…' : 'Retry submission'}</button>
    </section> : !allowed ? <p className="notice error">Only the household primary can check out this cart.</p> :
      !isAdult ? <p className="notice error">Checkout is available to adults aged 18 and older.</p> : <>
        <section className="panel checkout-form">
          <h2>Delivery addresses</h2>
          <div className="checkout-actions">
            {Object.keys(editors).map((kind) => <button key={kind} className="secondary" disabled={busy}
              onClick={() => { setEditor(editor === kind ? '' : kind); clearReview() }}>
              {editor === kind ? 'Done' : 'Manage addresses'}
            </button>)}
          </div>
          {editor ? editors[editor] : <>
            <label>{cart.kind === 'personal' ? 'Delivery address' : 'Default delivery address'}
              <select value={defaultAddress ? distinctAddress(defaultAddress) : ''} disabled={busy}
                onChange={(event) => { setSelected(event.target.value); clearReview() }}>
                {!defaultAddress && <option value="">Add a saved address to continue</option>}
                {addresses.map(option)}
              </select>
            </label>
            {cart.kind === 'household' && [...contributors].map(([id, rows]) => <label key={id}>
              {names.get(id) || id}<small className="muted">{rows.join(', ')}</small>
              <select aria-label={`Delivery address for ${names.get(id) || id}`}
                value={addressIndex.has(overrides[id]) ? overrides[id] : ''} disabled={busy}
                onChange={(event) => { setOverrides({ ...overrides, [id]: event.target.value }); clearReview() }}>
                <option value="">Use default address</option>
                {addresses.map(option)}
              </select>
            </label>)}
            <button disabled={busy || !defaultAddress} onClick={() => run(async () => {
              const reference = ({ kind, address_id }) => ({ kind, address_id })
              const submittedOverrides = [...contributors.keys()].flatMap((id) => {
                const address = addressIndex.get(overrides[id])
                return address ? [{ contributor_id: id, address: reference(address) }] : []
              })
              setQuote(null)
              setQuote(await account.quote({ cart, default_address: reference(defaultAddress), overrides: submittedOverrides }))
            })}>{busy && !quote ? 'Checking…' : 'Review order'}</button>
          </>}
        </section>
        {failedGroups && <DeliveryGroups groups={failedGroups} shortages={reviewError.detail.shortages} />}
        {quote && <section className="checkout-review">
          <h2>Order review</h2>
          <DeliveryGroups groups={quote.delivery_groups} />
          <div className="panel checkout-bill">
            <div><span>Delivery</span><strong>{amount(quote.delivery_fils)}</strong></div>
            <div><span>Total</span><strong>{amount(quote.total_fils)}</strong></div>
            {quote.mock_outcome_enabled && <label className="checkout-checkbox">
              <input type="checkbox" checked={decline} disabled={busy}
                onChange={(event) => setDecline(event.target.checked)} /> Simulate a payment decline
            </label>}
            <p className="muted">Demo payment. No charge.</p>
            <button disabled={busy} onClick={() => run(() => onPlace({
              key: crypto.randomUUID(),
              cart,
              body: { quote_id: quote.quote_id, ...(decline ? { mock_outcome: 'decline' } : {}) },
            }))}>{busy ? 'Placing order…' : 'Place order'}</button>
          </div>
        </section>}
      </>}
  </section>
}
