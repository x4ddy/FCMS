import React, { useState } from 'react'
import { Avatar, confirmAction, Empty, Icon, money, ProductImage, Stepper } from './ui.jsx'

function Tick({ label, checked, onChange }) {
  return <label className="tick">
    <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} aria-label={label} />
    <span aria-hidden="true"><Icon name="check" size={14} /></span>
  </label>
}

function NewCart({ busy, canAdultsOnly, onCreate }) {
  const [name, setName] = useState('')
  const [adultsOnly, setAdultsOnly] = useState(false)
  return <details className="fold">
    <summary><span><Icon name="plus" size={18} /> Create new cart</span></summary>
    <form onSubmit={(event) => {
      event.preventDefault()
      if (!name.trim()) return
      event.currentTarget.closest('details').open = false
      onCreate(name.trim(), adultsOnly)
      setName('')
      setAdultsOnly(false)
    }}>
      <div className="inline-form">
        <input aria-label="New cart name" placeholder="Cart name" required maxLength="100" value={name}
          onChange={(event) => setName(event.target.value)} />
        <button disabled={busy || !name.trim()}>Create</button>
      </div>
      {canAdultsOnly && <label className="checkbox-label"><input type="checkbox" checked={adultsOnly}
        onChange={(event) => setAdultsOnly(event.target.checked)} /> Adults only</label>}
    </form>
  </details>
}

function CartRow({ row, cart, user, names, isPrimary, selectable, selected, onSelect, locked, canRemove, remainingStock, stock, busy, onAdjust, onRemove }) {
  const label = row.product?.name ?? row.sku
  return <li className={`cart-row${selected ? ' selected' : ''}`}>
    {selectable && <Tick label={`Select ${label} for checkout`} checked={selected} onChange={onSelect} />}
    <ProductImage product={row.product} />
    <div className="cart-row-text">
      <strong>{label}</strong>
      {stock != null && row.quantity > stock && <small className="danger-text">{stock} available from {cart.inventory_store_name}</small>}
      <span className="people">{row.people.map((item) => {
        const name = names.get(item.contributor_id) ?? 'Member'
        return <span key={item.contributor_id}><Avatar id={item.contributor_id} name={name} size="tiny" />{item.contributor_id === user.id ? 'You' : name} × {item.quantity}</span>
      })}</span>
    </div>
    <div className="cart-row-end">
      <span className="price">{row.product ? money(row.product.price_fils * row.quantity) : 'Unavailable'}</span>
      <span className="cart-row-actions">
        {locked ? <strong className="quantity">× {row.quantity}</strong> : cart.can_edit_items && row.product && <Stepper label={label} quantity={row.quantity}
          canRemove={canRemove} remainingStock={remainingStock}
          removeHint="Only the admin can remove what others added" busy={busy} onAdjust={onAdjust} />}
        {isPrimary && !locked && <button className="icon-button" aria-label={`Remove ${label}`} title="Remove" onClick={onRemove}>
          <Icon name="trash" size={18} /></button>}
      </span>
    </div>
  </li>
}

function CartLocation({ cart, checkoutBy, addresses, preferredAddress, busy, onSetDelivery, onManageAddresses }) {
  const [editing, setEditing] = useState(false)
  const [chosen, setChosen] = useState('')
  const showingForm = cart.can_checkout && !checkoutBy && (editing || !cart.inventory_store_id)
  const address = addresses.find((item) => item.id === chosen && item.pinned)
    ?? addresses.find((item) => item.id === preferredAddress && item.pinned)
    ?? addresses.find((item) => item.pinned)
  return <section className="panel form-stack" aria-label="Cart delivery location">
    <div className="section-head">
      <strong>{cart.inventory_store_name ? `Delivery from ${cart.inventory_store_name}` : 'Delivery location required'}</strong>
      {cart.inventory_store_id && cart.can_checkout && !checkoutBy && <button type="button" className="text-button"
        onClick={() => setEditing(!editing)}>{editing ? 'Cancel' : 'Change'}</button>}
    </div>
    {!cart.inventory_store_id && !cart.can_checkout && <p className="muted">An eligible adult needs to set this cart’s delivery location.</p>}
    {showingForm && (address ? <form className="form-stack" onSubmit={async (event) => {
      event.preventDefault()
      if (await onSetDelivery(address.id)) setEditing(false)
    }}>
      <label>Delivery address
        <select value={address.id} disabled={busy} onChange={(event) => setChosen(event.target.value)}>
          {addresses.map((item) => <option key={item.id} value={item.id} disabled={!item.pinned}>
            {item.label} · {item.city}{item.pinned ? '' : ' · needs a map pin'}
          </option>)}
        </select>
      </label>
      <div className="row">
        <button type="submit" disabled={busy}>{cart.inventory_store_id ? 'Use this location' : 'Set delivery location'}</button>
        <button type="button" className="text-button" disabled={busy} onClick={onManageAddresses}>Manage addresses</button>
      </div>
    </form> : <p className="muted">Add a saved address with a map pin.{' '}
      <button type="button" className="text-button" onClick={onManageAddresses}>Manage addresses</button>
    </p>)}
  </section>
}

export function CartView({
  carts, cart, cartId, rows, waitingRows, user, household, members, names, grants, selected, busy, unresolved, live, canAdultsOnly,
  checkoutBy, mine, canRemove, onEndCheckout, onSelectCart, onToggle, onSelectAll, onAdjust, onRemove, onChangeGrant, onDeleteCart, onCreateCart, onCheckout,
  stock, quantities, inventoryFailed, onRetryInventory, addresses, preferredAddress, onSetDelivery, onManageAddresses,
}) {
  const isPrimary = household.role === 'primary'
  const available = rows.filter((row) => row.product)
  const chosen = available.filter((row) => selected.has(row.sku))
  const exceedsStock = chosen.some((row) => stock?.[row.sku] != null && row.quantity > stock[row.sku])
  const remainingStock = (sku) => stock ? (stock[sku] ?? 0) - (quantities[sku] ?? 0) : null
  const sum = (list) => list.reduce((total, row) => total + row.product.price_fils * row.quantity, 0)
  const units = (list) => list.reduce((total, row) => total + row.quantity, 0)
  const locked = Boolean(checkoutBy)
  const selectable = Boolean(cart?.can_checkout) && !locked
  const others = members.filter((member) => member.role !== 'primary' && (!cart?.adults_only || member.is_adult))
  // Members whose access is being saved. Their switch shows the saved state only, with a spinner meanwhile.
  const [savingAccess, setSavingAccess] = useState(() => new Set())
  async function changeAccess(member, grant) {
    setSavingAccess((current) => new Set(current).add(member))
    try { await onChangeGrant(member, grant) }
    finally { setSavingAccess((current) => { const next = new Set(current); next.delete(member); return next }) }
  }

  return <>
    <div className="drawer-body">
      <div className="cart-tabs" role="group" aria-label="Household carts">
        {carts.map((item) => <button key={item.id} aria-pressed={item.id === cartId} className="chip"
          onClick={() => onSelectCart(item.id)}>
          {item.adults_only && <Icon name="lock" size={14} />}{item.name}
          {item.id === cartId && rows.length > 0 ? ` (${units(rows)})` : ''}
        </button>)}
      </div>
      {isPrimary && <NewCart busy={busy} canAdultsOnly={canAdultsOnly} onCreate={onCreateCart} />}
      {carts.length === 0 ? <Empty icon="🛒" title="No cart yet">
        <p>{isPrimary ? 'Create a cart above to start.' : 'Ask the household admin for access to a cart.'}</p>
      </Empty> : !cart ? <div aria-busy="true">{[0, 1, 2].map((key) => <div className="cart-row skeleton" key={key} />)}</div> : <>
        <div className="section-head cart-title">
          <h3>{cart.name}</h3>
          <span className={`live-badge ${live ? 'on' : ''}`}>
            <span aria-hidden="true" />{live ? 'Live' : 'Reconnecting…'}</span>
        </div>
        <CartLocation key={cart.id} cart={cart} checkoutBy={checkoutBy} addresses={addresses} preferredAddress={preferredAddress}
          busy={busy} onSetDelivery={onSetDelivery} onManageAddresses={onManageAddresses} />
        {inventoryFailed && <div className="notice row" role="status">Stock check unavailable.
          <button className="secondary" onClick={onRetryInventory}>Retry</button></div>}
        {locked && <div className="banner row" role="status">
          <span><strong>{checkoutBy} {mine ? 'are' : 'is already'} checking out this cart</strong>
            <p>Anything added now waits for the next cart.</p></span>
          {(mine || isPrimary) && <button className="secondary" disabled={busy} onClick={onEndCheckout}>End checkout</button>}
        </div>}
        {rows.length === 0 ? <Empty icon="🧺" title="This cart is empty">
          <p>Add products and everyone with access will see them.</p>
        </Empty> : <>
          {selectable && available.length > 0 && <div className="select-all">
            <Tick label="Select all items" checked={chosen.length === available.length} onChange={onSelectAll} />
            <span aria-hidden="true">Select all items</span>
          </div>}
          <ul className="cart-rows">{rows.map((row) => <CartRow key={row.sku} row={row} cart={cart} user={user} names={names}
            isPrimary={isPrimary} selectable={selectable && Boolean(row.product)} selected={selectable && selected.has(row.sku)}
            onSelect={() => onToggle(row.sku)} locked={locked} canRemove={canRemove(row.sku)} busy={unresolved.has(row.sku)}
            remainingStock={remainingStock(row.sku)} stock={stock?.[row.sku]}
            onAdjust={(delta) => onAdjust(row.product, delta)} onRemove={() => onRemove(row)} />)}</ul>
        </>}
        {waitingRows.length > 0 && <>
          <div className="section-head cart-title"><h3>Waiting for the next cart</h3></div>
          <ul className="cart-rows waiting">{waitingRows.map((row) => <CartRow key={row.sku} row={row} cart={cart} user={user} names={names}
            isPrimary={isPrimary} canRemove={canRemove(row.sku)} remainingStock={remainingStock(row.sku)} stock={stock?.[row.sku]}
            busy={unresolved.has(row.sku)} onAdjust={(delta) => onAdjust(row.product, delta)}
            onRemove={() => onRemove(row)} />)}</ul>
        </>}
        {cart.can_manage_access && <details className="fold">
          <summary><span><Icon name="people" size={18} /> Who can use this cart</span></summary>
          {others.length === 0 ? <p className="muted">Nobody else can be given access yet.</p> : <ul className="plain-rows">
            {others.map((member) => {
              const granted = grants.includes(member.id)
              const saving = savingAccess.has(member.id)
              return <li key={member.id}>
                <span className="person"><Avatar id={member.id} name={member.display_name} />{member.display_name}</span>
                <span className="switch-cell">
                  {saving && <i className="switch-spinner" aria-hidden="true" />}
                  <button type="button" role="switch" aria-checked={granted} aria-busy={saving} aria-label={`Access for ${member.display_name}`}
                    className="switch" disabled={saving} onClick={() => changeAccess(member.id, !granted)}><span /></button>
                </span>
              </li>
            })}
          </ul>}
        </details>}
        {cart.can_delete_cart && <button className="text-button danger-text" disabled={busy} onClick={async () => {
          if (await confirmAction({
            title: `Delete “${cart.name}”?`, text: 'The cart and all its items are removed for everyone.', action: 'Delete cart', danger: true,
          })) onDeleteCart()
        }}><Icon name="trash" size={17} /> Delete this cart</button>}
      </>}
    </div>
    {cart && available.length > 0 && <footer className="drawer-footer">
      {selectable ? <>
        <div><strong>{units(chosen)} selected · {money(sum(chosen))}</strong>
          <small className="muted">{exceedsStock ? 'Reduce or deselect flagged items to continue.' : 'Unselected items stay in the cart.'}</small></div>
        <button className="big" disabled={busy || !cart.inventory_store_id || chosen.length === 0 || exceedsStock} onClick={onCheckout}>Checkout</button>
      </> : <div><strong>{units(available)} {units(available) === 1 ? 'item' : 'items'} · {money(sum(available))}</strong>
        <small className="muted">{locked ? 'Checkout is in progress.' : 'Only adult household members can check out.'}</small></div>}
    </footer>}
  </>
}
