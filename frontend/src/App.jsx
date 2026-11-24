import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  accountApi, connectCarts, googleConfiguration, householdApi, logoutSession, onAccessTokenChanged,
  refreshAccessToken, restoreSession, signInWithGoogle,
} from './api.js'
import { CartDetail } from './CartDetail.jsx'
import { AddressBook } from './AddressBook.jsx'
import { ShoppingLists } from './ShoppingLists.jsx'
import { applyCartSnapshot, cartActivity } from './cartState.js'
import { Checkout, OrderConfirmation } from './Checkout.jsx'
import { OrderHistory } from './OrderHistory.jsx'
import { isUnconfirmed, readAttempt, saveAttempt } from './checkoutState.js'
import './styles.css'

const money = new Intl.NumberFormat('en-AE', { style: 'currency', currency: 'AED' })

function sharedCartQuantities(contributions = []) {
  return contributions.reduce((quantities, item) => ({
    ...quantities,
    [item.sku]: (quantities[item.sku] ?? 0) + item.quantity,
  }), {})
}

function Storefront({ onOpenAuth }) {
  return (
    <section className="storefront" aria-label="FamilyCart grocery preview">
      <div className="hero-banner">
        <div>
          <h1>Everything your family needs</h1>
          <p>Fresh groceries, shared carts, delivered together.</p>
          <button onClick={onOpenAuth}>Start shopping</button>
        </div>
        <div className="hero-groceries" aria-hidden="true">🥬 🍎 🥛 🥚 🥖</div>
      </div>
      <div className="promo-grid">
        <article><span>💊</span><h2>Pharmacy at your doorstep</h2><p>Everyday wellness essentials</p></article>
        <article><span>🐶</span><h2>Pet care supplies</h2><p>Food, treats, toys and more</p></article>
        <article><span>🧸</span><h2>Baby care made easy</h2><p>Essentials for little ones</p></article>
      </div>
    </section>
  )
}

function DateOfBirthForm({ busy, onSubmit }) {
  const [birth, setBirth] = useState('')
  return <form className="birth-date-form" onSubmit={(event) => {
    event.preventDefault()
    onSubmit(birth)
  }}>
    <label>Date of birth<input type="date" autoComplete="bday" required value={birth}
      max={new Date().toISOString().slice(0, 10)} disabled={busy} onChange={(event) => setBirth(event.target.value)} /></label>
    <button disabled={busy}>Save and continue</button>
  </form>
}

function GoogleAuthModal({ busy, onClose, onAuthenticated }) {
  const buttonRef = useRef(null)
  const completeSignIn = useRef(onAuthenticated)
  completeSignIn.current = onAuthenticated
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [pendingCredential, setPendingCredential] = useState(null)
  const inFlight = useRef(false)
  const disabled = busy || submitting

  async function signIn(credential, birth) {
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setError('')
    try { await completeSignIn.current(await signInWithGoogle(credential, birth)) }
    catch (issue) {
      if (issue.code === 'DATE_OF_BIRTH_REQUIRED') setPendingCredential(credential)
      else {
        if (issue.status === 401 && birth) setPendingCredential(null)
        setError(issue.message)
      }
    } finally {
      setSubmitting(false)
      inFlight.current = false
    }
  }

  useEffect(() => {
    if (pendingCredential) return
    let cancelled = false
    const render = (clientId) => {
      if (cancelled || !buttonRef.current || !window.google?.accounts?.id) return
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: ({ credential }) => { if (!cancelled) signIn(credential) },
      })
      window.google.accounts.id.renderButton(buttonRef.current, {
        type: 'standard', theme: 'outline', size: 'large', shape: 'pill',
        text: 'continue_with', width: 320,
      })
    }
    googleConfiguration().then(({ client_id: clientId }) => {
      if (cancelled) return
      const existing = document.querySelector('script[data-familycart-google]')
      if (existing && window.google?.accounts?.id) render(clientId)
      else {
        const script = existing ?? document.createElement('script')
        script.src = 'https://accounts.google.com/gsi/client'
        script.async = true; script.defer = true
        script.dataset.familycartGoogle = 'true'
        script.onload = () => render(clientId)
        script.onerror = () => setError('Google sign-in could not be loaded.')
        if (!existing) document.head.appendChild(script)
      }
    }).catch((caught) => setError(caught.message))
    return () => { cancelled = true }
  }, [pendingCredential])

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !disabled) onClose()
    }}>
      <section className="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-title">
        <button className="modal-close" aria-label="Close login" onClick={onClose} disabled={disabled}>←</button>
        <div className="auth-logo">FC</div>
        <h1 id="auth-title">{pendingCredential ? 'Finish account setup' : 'Welcome to FamilyCart'}</h1>
        {pendingCredential ? <>
          <p>Enter your date of birth to finish setting up your account.</p>
          <DateOfBirthForm busy={disabled} onSubmit={(birth) => signIn(pendingCredential, birth)} />
          <button className="text-button" disabled={disabled}
            onClick={() => { setPendingCredential(null); setError('') }}>Use a different Google account</button>
        </> : <>
          <p>Log in or sign up with your Gmail account</p>
          <div className={disabled ? 'google-button disabled' : 'google-button'} ref={buttonRef} />
        </>}
        {error && <p className="auth-error" role="alert">{error}</p>}
        <small className="legal">By continuing, you agree to our Terms of service and Privacy policy.</small>
      </section>
    </div>
  )
}

function GroceryPage({
  products, busy, quantities, canEditCart, canOpenCart, householdCarts, activeCartId,
  onSelectCart, onAdjust, onOpenCart,
}) {
  const [query, setQuery] = useState('')
  const itemCount = Object.values(quantities).reduce((total, quantity) => total + quantity, 0)
  const cartTotal = products.reduce(
    (total, product) => total + (quantities[product.sku] ?? 0) * product.price_fils,
    0,
  )
  const groups = useMemo(() => {
    const search = query.trim().toLowerCase()
    const visible = products.filter((product) =>
      `${product.name} ${product.category}`.toLowerCase().includes(search),
    )
    const grouped = visible.reduce((result, product) => {
      result[product.category] ??= []
      result[product.category].push(product)
      return result
    }, {})
    return Object.entries(grouped)
  }, [products, query])

  return (
    <section className="grocery-page">
      <div className="shop-toolbar">
        <label className="grocery-search">
          <span aria-hidden="true">⌕</span>
          <input type="search" placeholder="Search groceries" value={query}
            onChange={(event) => setQuery(event.target.value)} />
        </label>
        {householdCarts.length > 0 && <label className="cart-selector">
          <span>Shopping cart</span>
          <select value={activeCartId ?? ''} onChange={(event) => onSelectCart(event.target.value)}>
            {householdCarts.map((cart) => <option key={cart.id} value={cart.id}>{cart.name}</option>)}
          </select>
        </label>}
        <button className="cart-summary" disabled={busy || itemCount === 0 || !canOpenCart} onClick={onOpenCart}
          aria-label={`Open cart with ${itemCount} items`}>
          <span className="cart-summary-icon" aria-hidden="true">🛒</span>
          <span><strong>{itemCount} {itemCount === 1 ? 'item' : 'items'}</strong>
            <small>{money.format(cartTotal / 100)}</small></span>
        </button>
      </div>
      {groups.length === 0 && <p className="empty">No groceries match your search.</p>}
      {groups.map(([category, items]) => (
        <section className="product-section" key={category}>
          <div className="section-title"><h2>{category}</h2><span>{items.length} products</span></div>
          <div className="shop-product-row">
            {items.map((product) => (
              <article className="shop-product-card" key={product.sku}>
                <img src={product.image_url} alt="" loading="lazy" />
                <small>◷ 19 MINS</small>
                <h3>{product.name}</h3>
                <p>{money.format(product.price_fils / 100)}</p>
                <div className="product-stepper" aria-label={`Quantity for ${product.name}`}>
                  <button className="secondary" disabled={busy || !canEditCart || !quantities[product.sku]}
                    onClick={() => onAdjust(product, -1)} aria-label={`Remove one ${product.name}`}>−</button>
                  <strong>{quantities[product.sku] ?? 0}</strong>
                  <button className="secondary" disabled={busy || !canEditCart}
                    onClick={() => onAdjust(product, 1)} aria-label={`Add one ${product.name}`}>+</button>
                </div>
              </article>
            ))}
          </div>
        </section>
      ))}
    </section>
  )
}

function PersonalCartView({ products, quantities, busy, onBack, onAdjust, onCheckout }) {
  const selected = products.filter((product) => (quantities[product.sku] ?? 0) > 0)
  const total = selected.reduce(
    (sum, product) => sum + product.price_fils * quantities[product.sku],
    0,
  )
  return (
    <section className="personal-cart-page">
      <button className="text-button" onClick={onBack}>← Continue shopping</button>
      <div className="page-heading">
        <div><h1>My cart</h1><p className="muted">Your personal shopping cart</p></div>
        <strong className="cart-grand-total">{money.format(total / 100)}</strong>
      </div>
      {selected.length === 0 ? <p className="empty">Your cart is empty.</p> : (
        <div className="personal-cart-layout">
          <ul className="personal-cart-items">
            {selected.map((product) => {
              const quantity = quantities[product.sku]
              return <li key={product.sku}>
                <img src={product.image_url} alt="" />
                <div className="personal-cart-product">
                  <strong>{product.name}</strong>
                  <small>{money.format(product.price_fils / 100)} each</small>
                </div>
                <div className="product-stepper">
                  <button className="secondary" disabled={busy}
                    onClick={() => onAdjust(product, -1)} aria-label={`Remove one ${product.name}`}>−</button>
                  <strong>{quantity}</strong>
                  <button className="secondary" disabled={busy}
                    onClick={() => onAdjust(product, 1)} aria-label={`Add one ${product.name}`}>+</button>
                </div>
                <strong className="line-total">{money.format(product.price_fils * quantity / 100)}</strong>
              </li>
            })}
          </ul>
          <aside className="panel cart-bill">
            <h2>Bill details</h2>
            <div><span>Items total</span><strong>{money.format(total / 100)}</strong></div>
            <div><span>Delivery</span><strong>Calculated at checkout</strong></div>
            <div className="bill-total"><span>Total</span><strong>{money.format(total / 100)}</strong></div>
            <button disabled={busy} onClick={onCheckout}>Checkout</button>
          </aside>
        </div>
      )}
    </section>
  )
}

function HouseholdSetup({ busy, displayName, onCreate, onJoin }) {
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  return (
    <div className="setup-grid">
      <form className="panel" onSubmit={(event) => {
        event.preventDefault()
        onCreate(name)
      }}>
        <h2>Create a household</h2>
        <p className="muted">Start a shared account for carts, members, and delivery addresses.</p>
        <label>Household name<input required maxLength="100" placeholder="The Sharma Family"
          value={name} onChange={(event) => setName(event.target.value)} /></label>
        <button disabled={busy || !name.trim()}>Create household</button>
      </form>
      <form className="panel" onSubmit={(event) => {
        event.preventDefault()
        onJoin(code, displayName)
      }}>
        <h2>Join a household</h2>
        <p className="muted">Paste the private invitation code shared by a household primary.</p>
        <label>Invitation code<input required maxLength="200" placeholder="Paste invitation code"
          value={code} onChange={(event) => setCode(event.target.value)} /></label>
        <button disabled={busy || !code.trim()}>Join household</button>
      </form>
    </div>
  )
}

function PersonalAddresses({ required = false, onBack, ...addressBook }) {
  return <section className="household-dashboard">
    <div className="page-heading"><div>
      {!required && <button className="text-button" onClick={onBack}>Back to groceries</button>}
      <h1>{required ? 'Add your delivery address' : 'Your addresses'}</h1>
      <p className="muted">{required
        ? 'A delivery address is required before you start shopping.'
        : 'Save multiple addresses and choose one when placing an order.'}</p>
    </div></div>
    <section className="panel dashboard-addresses">
      <AddressBook {...addressBook} buttonLabel={required ? 'Save and start shopping' : 'Add address'} />
    </section>
  </section>
}

function HouseholdDashboard({
  household, households, session, carts, members, addresses, busy, invitation, onBack, onCreate,
  onJoin, onSelectHousehold, onInvite, onRevoke, onTransferHousehold, onAddress,
  onDeleteAddress, onUpdatePin, addressApi, onOpenCart, onShopCart, onCreateCart, onDeleteCart, onOpenLists,
}) {
  const [cartName, setCartName] = useState('')
  const [adultsOnly, setAdultsOnly] = useState(false)
  return (
    <section className="household-dashboard">
      <div className="page-heading">
        <div><button className="text-button" onClick={onBack}>← Back to groceries</button>
          <h1>{household?.name ?? 'Household account'}</h1>
          {households.length > 1 && <label className="household-switcher">Active household
            <select value={household?.id ?? ''} onChange={(event) => onSelectHousehold(event.target.value)}>
              {households.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>}
        </div>
        {household && <span className="role-badge">{household.role}</span>}
      </div>
      {!household ? (
        <HouseholdSetup busy={busy} displayName={session.user.display_name} onCreate={onCreate} onJoin={onJoin} />
      ) : (
        <div className="dashboard-grid">
          <section className="panel dashboard-members">
            <div className="section-title"><h2>Family members</h2>
              {household.role === 'primary' && <button onClick={onInvite} disabled={busy}>Invite member</button>}
            </div>
            {invitation && <div className="invite-code">
              <small>Private one-time invitation code</small><code>{invitation.invitation_code}</code>
              <button className="secondary" onClick={() => navigator.clipboard.writeText(invitation.invitation_code)}>Copy</button>
            </div>}
            <ul className="rows">
              {members.map((member) => <li key={member.id}>
                <div><strong>{member.display_name}</strong><small>{member.role}</small></div>
                {household.role === 'primary' && member.role !== 'primary' && <div className="row-actions">
                  <button className="secondary" disabled={busy} onClick={() => {
                    if (window.confirm(`Transfer household ownership to ${member.display_name}?`)) {
                      onTransferHousehold(member.id)
                    }
                  }}>Make primary</button>
                  <button className="secondary danger" disabled={busy} onClick={() => onRevoke(member.id)}>Revoke</button>
                </div>}
              </li>)}
            </ul>
          </section>
          <section className="panel dashboard-carts">
            <h2>Shared shopping carts</h2>
            {household.role === 'primary' && <form className="new-household-cart" onSubmit={(event) => {
              event.preventDefault()
              if (!cartName.trim()) return
              onCreateCart(cartName.trim(), adultsOnly)
              setCartName('')
              setAdultsOnly(false)
            }}>
              <input aria-label="New cart name" placeholder="New cart name" required maxLength="100"
                value={cartName} onChange={(event) => setCartName(event.target.value)} />
              {session.can_create_adults_only_cart && <label className="checkbox-label">
                <input type="checkbox" checked={adultsOnly}
                  onChange={(event) => setAdultsOnly(event.target.checked)} /> Adults only
              </label>}
              <button disabled={busy || !cartName.trim()}>Create cart</button>
            </form>}
            <ul className="rows">
              {carts.map((cart) => <li key={cart.id}>
                <div><strong>{cart.name}</strong>
                  <small>{cart.adults_only ? 'Adults only' : 'Shared cart'}</small>
                </div>
                <div className="row-actions">
                  <button onClick={() => onShopCart(cart.id)}>Shop</button>
                  <button className="secondary" onClick={() => onOpenCart(cart.id)}>View</button>
                  {household.role === 'primary' && <button className="secondary danger" disabled={busy}
                    onClick={() => {
                      if (window.confirm(`Delete “${cart.name}” and all its items?`)) onDeleteCart(cart.id)
                    }}>Delete</button>}
                </div>
              </li>)}
            </ul>
          </section>
          <section className="panel dashboard-lists">
            <div>
              <h2>Shared shopping lists</h2>
              <p className="muted">Plan together, tick items off, then send them to a cart in one tap.</p>
            </div>
            <button onClick={onOpenLists}>Open lists →</button>
          </section>
          <section className="panel dashboard-addresses">
            <h2>Shared delivery addresses</h2>
            <AddressBook key={household.id} addresses={addresses} api={addressApi} busy={busy}
              displayName={session.user.display_name} onSave={onAddress} onDelete={onDeleteAddress}
              onUpdatePin={onUpdatePin} canEdit={(address) => household.role === 'primary' || address.created_by === session.user.id} />
          </section>
          <details className="panel household-more">
            <summary>Create or join another household</summary>
            <HouseholdSetup busy={busy} displayName={session.user.display_name}
              onCreate={onCreate} onJoin={onJoin} />
          </details>
        </div>
      )}
    </section>
  )
}

function orderFromLocation() {
  return window.location.pathname.match(/^\/orders\/([a-z0-9-]+)\/?$/i)?.[1] ?? null
}

function orderHistoryFromLocation() {
  return /^\/orders\/?$/.test(window.location.pathname)
}

function App() {
  const [session, setSession] = useState(null)
  const [restoringSession, setRestoringSession] = useState(true)
  const [authOpen, setAuthOpen] = useState(false)
  const [view, setView] = useState('shop')
  const [household, setHousehold] = useState(null)
  const [households, setHouseholds] = useState([])
  const [carts, setCarts] = useState([])
  const [products, setProducts] = useState([])
  const [members, setMembers] = useState([])
  const [addresses, setAddresses] = useState([])
  const [personalAddresses, setPersonalAddresses] = useState([])
  const [quantities, setQuantities] = useState({})
  const [personalQuantities, setPersonalQuantities] = useState({})
  const [invitation, setInvitation] = useState(null)
  const [detail, setDetail] = useState(null)
  const [shoppingCartId, setShoppingCartId] = useState(null)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const [cartNotices, setCartNotices] = useState([])
  const [checkoutCart, setCheckoutCart] = useState(null)
  const [pendingOrder, setPendingOrder] = useState(null)
  const [orderId, setOrderId] = useState(orderFromLocation)
  const [showOrderHistory, setShowOrderHistory] = useState(orderHistoryFromLocation)
  const [confirmation, setConfirmation] = useState(null)
  const [orderError, setOrderError] = useState(null)
  const noticeSequence = useRef(0)
  const productNames = useRef(new Map())
  const currentUser = useRef(null)
  // sku -> { delta, timer, product }: + / − taps waiting to be sent as one request.
  const pendingTaps = useRef(new Map())
  const token = session?.access_token
  const needsAgeSetup = session?.user.age_setup_required
  const cartId = detail?.cart.id
  const liveCartId = cartId ?? shoppingCartId
  const householdId = household?.id
  const account = useMemo(() => token ? accountApi(token) : null, [token])
  const api = useMemo(() => token && householdId ? householdApi(token, householdId) : null, [token, householdId])
  const displayQuantities = household ? quantities : personalQuantities

  useEffect(() => {
    productNames.current = new Map(products.map((product) => [product.sku, product.name]))
  }, [products])

  function showCartActivity(messages, cartName) {
    messages.forEach((message) => showPopup(`${message} in ${cartName}.`))
  }

  function showPopup(text, kind = 'activity') {
    const notice = { id: ++noticeSequence.current, text, kind }
    setCartNotices((current) => [...current, notice].slice(-4))
    window.setTimeout(() => {
      const { id } = notice
      setCartNotices((current) => current.filter((notice) => notice.id !== id))
    }, 7000)
  }

  function clearSession() {
    currentUser.current = null
    setSession(null); setHousehold(null); setHouseholds([]); setCarts([]); setProducts([]); setMembers([])
    setAddresses([]); setPersonalAddresses([]); setQuantities({}); setInvitation(null); setDetail(null); setShoppingCartId(null)
    setPersonalQuantities({}); setCheckoutCart(null); setPendingOrder(null); setConfirmation(null); setOrderError(null)
    setCartNotices([]); setView('shop')
  }

  useEffect(() => onAccessTokenChanged((nextToken, refreshedUser) => {
    if (!nextToken) clearSession()
    else if (refreshedUser && currentUser.current && refreshedUser.id !== currentUser.current) clearSession()
    else setSession((current) => current && {
      ...current, access_token: nextToken,
      user: refreshedUser ? { ...current.user, is_adult: refreshedUser.is_adult } : current.user,
    })
  }), [])

  async function loadHousehold(selected, accessToken = token, user = session?.user) {
    if (!selected) {
      setHousehold(null); setCarts([]); setMembers([]); setAddresses([]); setShoppingCartId(null)
      return
    }
    const selectedApi = householdApi(accessToken, selected.id)
    const [visibleCarts, capabilities, savedAddresses] = await Promise.all([
      selectedApi.carts(), selectedApi.capabilities(), selectedApi.addresses(),
    ])
    const visibleMembers = selected.role === 'primary' ? await selectedApi.members() : []
    setHousehold(selected); setCarts(visibleCarts); setMembers(visibleMembers); setAddresses(savedAddresses)
    const initialCart = visibleCarts.find((cart) => !cart.adults_only) ?? visibleCarts[0]
    setShoppingCartId(initialCart?.id ?? null)
    if (initialCart) {
      const cart = await selectedApi.cart(initialCart.id)
      setQuantities(sharedCartQuantities(cart.contributions))
    } else {
      setQuantities({})
    }
    setSession((current) => current && { ...current, ...capabilities })
  }

  async function hydrateSession(signedIn) {
    if (signedIn.user.age_setup_required) {
      currentUser.current = signedIn.user.id
      setSession(signedIn)
      return
    }
    const signedInAccount = accountApi(signedIn.access_token)
    const [visibleHouseholds, visibleProducts, personalCart, savedPersonalAddresses] = await Promise.all([
      signedInAccount.households(), signedInAccount.products(), signedInAccount.personalCart(),
      signedInAccount.addresses(),
    ])
    currentUser.current = signedIn.user.id
    setSession(signedIn)
    setHouseholds(visibleHouseholds)
    setPersonalAddresses(savedPersonalAddresses)
    setProducts(visibleProducts)
    setPersonalQuantities(Object.fromEntries(personalCart.items.map((item) => [item.sku, item.quantity])))
    setPendingOrder(readAttempt(signedIn.user.id))
    await loadHousehold(visibleHouseholds[0] ?? null, signedIn.access_token, signedIn.user)
    if (savedPersonalAddresses.length === 0) setView('address-onboarding')
  }

  useEffect(() => {
    let cancelled = false
    restoreSession()
      .then((signedIn) => cancelled ? undefined : hydrateSession(signedIn))
      .catch((error) => {
        if (cancelled) return
        clearSession()
        if (error.status !== 401) {
          setFeedback({ error: true, text: 'Your session could not be restored right now. Reload the page to try again.' })
        }
      })
      .finally(() => {
        if (!cancelled) setRestoringSession(false)
      })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!token || !household) return
    let stopped = false
    let reconnect
    let socket
    let previousContributions = null
    function connect() {
      socket = connectCarts(token, household.id, liveCartId)
      socket.onmessage = (event) => {
        if (stopped) return
        const snapshot = JSON.parse(event.data)
        setCarts(snapshot.carts)
        if (snapshot.household_role) {
          setHousehold((current) => current && { ...current, role: snapshot.household_role })
          setHouseholds((current) => current.map((item) => item.id === household.id
            ? { ...item, role: snapshot.household_role }
            : item))
          setSession((current) => current && {
            ...current,
            can_create_adults_only_cart: snapshot.can_create_adults_only_cart,
          })
        }
        if (snapshot.members) setMembers(snapshot.members)
        if (snapshot.cart === null && liveCartId) {
          previousContributions = null
          if (cartId) setDetail(null)
          else { setShoppingCartId(null); setQuantities({}) }
          if (snapshot.access_notice) showPopup(snapshot.access_notice, 'error')
        } else if (snapshot.cart) {
          if (previousContributions !== null) {
            const memberNames = new Map((snapshot.members ?? []).map((member) => [member.id, member.display_name]))
            showCartActivity(cartActivity(
              previousContributions,
              snapshot.cart.contributions,
              session.user.id,
              snapshot.actor_id,
              memberNames,
              productNames.current,
              snapshot.actor_ids,
            ), snapshot.cart.name)
          }
          previousContributions = snapshot.cart.contributions
          if (cartId) setDetail((current) => {
            const next = applyCartSnapshot(current, snapshot.cart, snapshot.revision)
            return next !== current && snapshot.grants ? { ...next, grants: snapshot.grants } : next
          })
          else setQuantities(withPendingTaps(sharedCartQuantities(snapshot.cart.contributions)))
        }
      }
      socket.onclose = async ({ code, reason }) => {
        if (stopped) return
        if (code === 4401) {
          // Access tokens expire after 10 minutes. Refreshing publishes the new
          // token, which causes this effect to reconnect the socket securely.
          try { await refreshAccessToken() } catch (error) {
            // On 401 the token listener clears the session; otherwise try again.
            if (error.status !== 401 && !stopped) reconnect = window.setTimeout(connect, 2000)
          }
        }
        else if (code === 4404) {
          const personalCart = await accountApi(token).personalCart().catch(() => ({ items: [] }))
          setHousehold(null); setCarts([]); setMembers([]); setAddresses([]); setDetail(null)
          setPersonalQuantities(Object.fromEntries(personalCart.items.map((item) => [item.sku, item.quantity])))
          setView('shop')
          showPopup(reason || 'Your household access was revoked. Contact the primary account holder.', 'error')
        }
        else reconnect = window.setTimeout(connect, 1500)
      }
    }
    connect()
    return () => { stopped = true; window.clearTimeout(reconnect); socket?.close() }
  // cartId distinguishes the grocery view from the open cart-detail view.
  // liveCartId can be identical in both, so it must be a separate dependency
  // or the socket handler keeps writing to the old view until a page reload.
  }, [token, household?.id, liveCartId, cartId])

  // Always follow a cart that still exists. If the cart being shopped is deleted
  // or access to it is removed, fall back to the household's regular shared cart.
  // Without this the live connection follows no cart: edits still save, but the
  // screen stops showing other members' items and their notifications.
  useEffect(() => {
    if (!household || carts.length === 0) return
    if (carts.some((cart) => cart.id === shoppingCartId)) return
    const fallback = carts.find((cart) => !cart.adults_only) ?? carts[0]
    // Changing the followed cart reconnects the live socket, whose first
    // message carries that cart's current contents.
    setShoppingCartId(fallback.id)
  }, [household?.id, carts, shoppingCartId])

  useEffect(() => {
    function handlePopState() {
      setOrderId(orderFromLocation())
      setShowOrderHistory(orderHistoryFromLocation())
      setCheckoutCart(null)
      setConfirmation(null)
      setOrderError(null)
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  useEffect(() => {
    if (!account || needsAgeSetup || !orderId || confirmation?.order_id === orderId) return
    const controller = new AbortController()
    setOrderError(null)
    account.order(orderId, controller.signal).then((order) => {
      if (!controller.signal.aborted && currentUser.current === order.purchaser_id) setConfirmation(order)
    }, (error) => {
      if (!controller.signal.aborted) setOrderError(error.message)
    })
    return () => controller.abort()
  }, [account, needsAgeSetup, orderId, confirmation?.order_id])

  function openPage(next) {
    if (next === 'orders') {
      if (!orderHistoryFromLocation()) window.history.pushState(null, '', '/orders')
    } else if (orderFromLocation() || orderHistoryFromLocation()) window.history.pushState(null, '', '/')
    setOrderId(null)
    setShowOrderHistory(next === 'orders')
    setOrderError(null)
    setDetail(null)
    setCheckoutCart(null)
    setFeedback(null)
    setView(next)
  }

  function startCheckout(cart) {
    setFeedback(null)
    setCheckoutCart(cart)
  }

  function openOrder(id) {
    setCheckoutCart(null)
    setOrderId(id)
    setShowOrderHistory(false)
    setConfirmation(null)
    setOrderError(null)
    window.history.pushState(null, '', `/orders/${id}`)
  }

  function resolveOrder(id) {
    openOrder(id)
    saveAttempt(session.user.id, null)
    setPendingOrder(null)
  }

  async function submitOrder(attempt) {
    const userId = session.user.id
    saveAttempt(userId, attempt)
    setPendingOrder(attempt)
    let placed
    try {
      placed = await account.placeOrder(attempt.body, attempt.key)
    } catch (error) {
      if (!isUnconfirmed(error)) {
        saveAttempt(userId, null)
        if (currentUser.current === userId) setPendingOrder(null)
      }
      throw error
    }
    if (currentUser.current !== userId) return
    resolveOrder(placed.order_id)
    setConfirmation(placed)
    if (attempt.cart.kind === 'personal') {
      setPersonalQuantities({})
      account.personalCart().then(({ items }) => {
        if (currentUser.current === userId) {
          setPersonalQuantities(Object.fromEntries(items.map((item) => [item.sku, item.quantity])))
        }
      }).catch(() => {})
    } else if (attempt.cart.household_id === householdId) {
      const placedCart = attempt.cart.cart_id
      setDetail((current) => current?.cart.id === placedCart
        ? { ...current, cart: { ...current.cart, contributions: [] } } : current)
      if (shoppingCartId === placedCart) setQuantities({})
      api.cart(placedCart).then((cart) => {
        if (currentUser.current !== userId) return
        setDetail((current) => current?.cart.id === placedCart ? { ...current, cart } : current)
        if (shoppingCartId === placedCart) setQuantities(sharedCartQuantities(cart.contributions))
      }).catch(() => {})
    }
  }

  async function run(action) {
    setBusy(true); setFeedback(null)
    try { return await action() }
    catch (error) {
      if (error.status === 401) clearSession()
      if (error.status === 403) showPopup(error.message, 'error')
      else setFeedback({ error: true, text: error.message })
    } finally { setBusy(false) }
  }

  function updatePin(api, setAddresses, id, pin) {
    return run(async () => {
      const saved = await api.updateAddressPin(id, pin)
      setAddresses((current) => current.map((address) => address.id === id ? saved : address))
      return saved
    })
  }

  async function openCart(id) {
    const cart = await api.cart(id)
    const grants = cart.can_manage_access ? await api.grants(id) : []
    setDetail({ cart, grants, snapshotRevision: 0, pendingEdits: new Map() })
  }

  async function selectShoppingCart(id) {
    const cart = await api.cart(id)
    setShoppingCartId(id)
    setQuantities(sharedCartQuantities(cart.contributions))
    setDetail(null)
    setView('shop')
  }

  // Fast + / − taps on the same product are combined into one request. Each entry
  // remembers its cart: 'shared' (the household cart being shopped) or 'personal'.
  const tapKey = (target, sku) => `${target}:${sku}`

  function waitingTaps(target, sku) {
    return pendingTaps.current.get(tapKey(target, sku))?.delta ?? 0
  }

  /** Add taps that have not been sent yet on top of quantities from the server. */
  function withPendingTaps(quantities, target = 'shared') {
    const next = { ...quantities }
    for (const entry of pendingTaps.current.values()) {
      if (entry.target !== target) continue
      next[entry.product.sku] = Math.max(0, (next[entry.product.sku] ?? 0) + entry.delta)
    }
    return next
  }

  /** Show a tap immediately; send all taps on the same product as one request after a short pause. */
  function queueAdjust(product, delta, target = household ? 'shared' : 'personal') {
    const key = tapKey(target, product.sku)
    const entry = pendingTaps.current.get(key) ?? { delta: 0, product, target }
    entry.delta = Math.max(-99, Math.min(99, entry.delta + delta))
    const setDisplayed = target === 'personal' ? setPersonalQuantities : setQuantities
    setDisplayed((current) => ({ ...current, [product.sku]: Math.max(0, (current[product.sku] ?? 0) + delta) }))
    window.clearTimeout(entry.timer)
    entry.timer = window.setTimeout(() => flushTaps(key), 400)
    pendingTaps.current.set(key, entry)
  }

  async function flushTaps(key) {
    const entry = pendingTaps.current.get(key)
    pendingTaps.current.delete(key)
    if (!entry || entry.delta === 0) return
    try {
      if (entry.target === 'personal') await adjustPersonalProduct(entry.product, entry.delta)
      else await adjustProduct(entry.product, entry.delta)
    } catch (error) {
      if (error.status === 401) { clearSession(); return }
      if (error.status === 403) showPopup(error.message, 'error')
      else setFeedback({ error: true, text: error.message })
      // Put the screen back in line with what was actually saved.
      try {
        if (entry.target === 'shared' && household && shoppingCartId) {
          const cart = await api.cart(shoppingCartId)
          setQuantities(withPendingTaps(sharedCartQuantities(cart.contributions), 'shared'))
        } else if (entry.target === 'personal' && account) {
          const personalCart = await account.personalCart()
          setPersonalQuantities(withPendingTaps(
            Object.fromEntries(personalCart.items.map((item) => [item.sku, item.quantity])), 'personal',
          ))
        }
      } catch { /* the next live update will correct it */ }
    }
  }

  async function adjustProduct(product, delta) {
    if (!household) return adjustPersonalProduct(product, delta)
    let cart = carts.find((candidate) => candidate.id === shoppingCartId)
      ?? carts.find((candidate) => !candidate.adults_only)
    if (!cart) {
      cart = await api.create('Shared shopping cart', false)
      setCarts(await api.carts())
      setShoppingCartId(cart.id)
    }
    const update = await api.adjustSharedQuantity(cart.id, product.sku, delta)
    // Taps made while this request was running are still waiting; keep them on top.
    const waiting = waitingTaps('shared', product.sku)
    setQuantities((current) => ({ ...current, [product.sku]: Math.max(0, update.quantity + waiting) }))
  }

  async function adjustPersonalProduct(product, delta) {
    const update = await account.adjustPersonalItem(product.sku, delta)
    const waiting = waitingTaps('personal', product.sku)
    setPersonalQuantities((current) => ({ ...current, [product.sku]: Math.max(0, update.quantity + waiting) }))
  }

  function checkoutAddressBook(addressApi, saved, setSaved) {
    return <AddressBook addresses={saved} api={addressApi} busy={busy} displayName={session.user.display_name}
      onSave={(address) => run(async () => {
        const created = await addressApi.createAddress(address)
        setSaved((current) => [...current, created])
        return created
      })}
      onUpdatePin={(id, pin) => updatePin(addressApi, setSaved, id, pin)}
      onDelete={(id) => run(async () => {
        await addressApi.deleteAddress(id)
        setSaved(await addressApi.addresses())
      })} />
  }

  const sharedCheckout = checkoutCart?.kind === 'household'
  function checkoutAddresses(saved, kind) {
    return saved.map((address) => ({
      ...address, kind, address_id: address.id, pinned: address.latitude != null && address.longitude != null,
    }))
  }

  async function updateItem(save) {
    await save()
    const refreshed = await api.cart(cartId)
    setDetail((current) => current && { ...current, cart: refreshed, pendingEdits: new Map() })
  }

  async function refreshMembers() {
    if (household?.role === 'primary') setMembers(await api.members())
  }

  return (
    <>
      <header className="site-header">
        <div className="shell header-content">
          <button className="brand-button" onClick={() => openPage('shop')}>familycart</button>
          {session && <div className="header-spacer" />}
          {session ? <div className="account"><span>{session.user.display_name}</span>
            {!needsAgeSetup && pendingOrder && <button className="secondary" onClick={() => {
              openPage('shop')
              startCheckout(pendingOrder.cart)
            }}>Resume checkout</button>}
            {!needsAgeSetup && <>
              <button className="secondary" onClick={() => openPage('personal-cart')}>My cart</button>
              <button className="secondary" onClick={() => openPage('orders')}>Order history</button>
              <button className="secondary" onClick={() => openPage('addresses')}>Addresses</button>
            </>}
            <button className="secondary" disabled={busy} onClick={() => run(async () => {
              await logoutSession(token)
              clearSession()
            })}>Sign out</button></div> :
            !restoringSession && <button className="header-login" onClick={() => setAuthOpen(true)}>Login / Register</button>}
          {session && household && !needsAgeSetup && <button className="header-lists" onClick={() => openPage('lists')}>
            📝 Lists
          </button>}
          {session && !needsAgeSetup && <button className="header-household" onClick={() => openPage('household')}>
            {household ? 'Household account' : '+ Create household'}
          </button>}
        </div>
      </header>
      {cartNotices.length > 0 && <div className="cart-activity-stack" aria-live="polite" aria-label="Shared cart activity">
        {cartNotices.map((notice) => <div
          className={`cart-activity-notice ${notice.kind === 'error' ? 'error' : ''}`} key={notice.id}>
          <span aria-hidden="true">🛒</span><strong>{notice.text}</strong>
          <button type="button" aria-label="Dismiss notification"
            onClick={() => setCartNotices((current) => current.filter((item) => item.id !== notice.id))}>×</button>
        </div>)}
      </div>}
      <main className="shell">
        {feedback && <p className={feedback.error ? 'notice error' : 'notice'}>{feedback.text}</p>}
        {restoringSession ? <p className="session-loading" role="status">Restoring your session…</p> :
          !session ? <Storefront onOpenAuth={() => setAuthOpen(true)} /> :
          needsAgeSetup ? <section className="panel onboarding-card">
            <h1>Finish account setup</h1>
            <p>Add your date of birth to complete your account.</p>
            <DateOfBirthForm busy={busy} onSubmit={(birth) => run(async () => {
              try { await account.saveAge(birth) }
              catch (issue) { if (issue.status !== 409) throw issue }
              await hydrateSession(await restoreSession())
            })} />
          </section> :
          showOrderHistory ?
            <OrderHistory key={session.user.id} account={account} onOpen={openOrder} /> :
          orderId ?
            <OrderConfirmation order={confirmation?.order_id === orderId && confirmation.purchaser_id === session.user.id
              ? confirmation : null}
              error={orderError} onBack={() => openPage('orders')} /> :
          checkoutCart ?
            <Checkout account={account} cart={checkoutCart} pending={pendingOrder} onPlace={submitOrder}
              onOpenOrder={resolveOrder} isAdult={session.user.is_adult}
              allowed={!sharedCheckout || (householdId === checkoutCart.household_id && household.role === 'primary'
                && detail?.cart.id === checkoutCart.cart_id && detail.cart.can_manage_access)}
              contributions={sharedCheckout ? detail?.cart.contributions ?? [] : []}
              members={members} products={products}
              addresses={[...checkoutAddresses(personalAddresses, 'personal'),
                ...(sharedCheckout ? checkoutAddresses(addresses, 'household') : [])]}
              editors={{
                personal: checkoutAddressBook(account, personalAddresses, setPersonalAddresses),
                ...(sharedCheckout && api ? { household: checkoutAddressBook(api, addresses, setAddresses) } : {}),
              }}
              onBack={() => { setCheckoutCart(null); setFeedback(null) }} /> :
          personalAddresses.length === 0 ?
            <PersonalAddresses addresses={personalAddresses} api={account} displayName={session.user.display_name} busy={busy} required
              onSave={(address) => run(async () => {
                const saved = await account.createAddress(address)
                setPersonalAddresses((current) => [...current, saved])
                setView('shop')
                setFeedback({ text: 'Address saved. You can now start shopping.' })
                return saved
              })} /> :
          view === 'addresses' ?
            <PersonalAddresses addresses={personalAddresses} api={account} displayName={session.user.display_name} busy={busy}
              onBack={() => setView('shop')}
              onSave={(address) => run(async () => {
                const saved = await account.createAddress(address)
                setPersonalAddresses((current) => [...current, saved])
                setFeedback({ text: 'Address added.' })
                return saved
              })}
              onUpdatePin={(id, pin) => updatePin(account, setPersonalAddresses, id, pin)}
              onDelete={(id) => run(async () => {
                await account.deleteAddress(id)
                setPersonalAddresses(await account.addresses())
              })} /> :
          detail ? <>
            <button className="secondary back" onClick={() => setDetail(null)}>← Back to shopping</button>
            <CartDetail key={detail.cart.id} {...detail} user={session.user} busy={busy} products={products} members={members}
              onCheckout={() => startCheckout({ kind: 'household', household_id: householdId, cart_id: cartId })}
              onAdjustQuantity={(sku, delta) => run(() => updateItem(() => api.adjustSharedQuantity(cartId, sku, delta)))}
              onRemoveItem={(sku) => run(() => updateItem(() => api.removeSharedItem(cartId, sku)))}
              onChangeGrant={(member, grant) => run(async () => {
                await api.changeGrant(cartId, member, grant)
                const grants = await api.grants(cartId)
                setDetail((current) => ({ ...current, grants }))
              })}
              onDeleteCart={() => run(async () => {
                await api.deleteCart(cartId)
                const remaining = await api.carts()
                setDetail(null); setCarts(remaining)
                if (shoppingCartId === cartId) {
                  setShoppingCartId(remaining[0]?.id ?? null)
                  setQuantities({})
                }
              })} />
          </> : view === 'lists' && household ?
            <ShoppingLists api={api} token={token} household={household} user={session.user} products={products}
              carts={carts} defaultCartId={shoppingCartId} onBack={() => setView('shop')}
              onNotify={(text, kind) => showPopup(text, kind)}
              onCartChanged={async (id) => {
                if (id === shoppingCartId) {
                  const cart = await api.cart(id).catch(() => null)
                  if (cart) setQuantities(sharedCartQuantities(cart.contributions))
                }
              }}
              onAccessLost={(reason) => {
                setView('shop')
                showPopup(reason || 'Your household access was revoked. Contact the primary account holder.', 'error')
              }} />
          : view === 'personal-cart' ?
            <PersonalCartView products={products} quantities={personalQuantities} busy={busy}
              onBack={() => setView('shop')}
              onCheckout={() => startCheckout({ kind: 'personal' })}
              onAdjust={(product, delta) => queueAdjust(product, delta, 'personal')} /> :
          view === 'household' ?
            <HouseholdDashboard household={household} households={households} session={session} carts={carts} members={members}
              addresses={addresses} addressApi={api} busy={busy} invitation={invitation} onBack={() => setView('shop')}
              onCreate={(name) => run(async () => {
                const created = await account.createHousehold(name, session.user.display_name)
                setHouseholds(await account.households())
                await loadHousehold(created); setFeedback({ text: 'Household created.' })
              })}
              onJoin={(code) => run(async () => {
                const joined = await account.acceptInvitation(code, session.user.display_name)
                setHouseholds(await account.households())
                await loadHousehold(joined); setFeedback({ text: 'You joined the household.' })
              })}
              onSelectHousehold={(id) => run(() => loadHousehold(households.find((item) => item.id === id)))}
              onInvite={() => run(async () => setInvitation(await api.createInvitation()))}
              onRevoke={(id) => run(async () => { await api.revokeMember(id); await refreshMembers() })}
              onTransferHousehold={(id) => run(async () => {
                await api.transferHouseholdOwnership(id)
                const updated = await account.households()
                setHouseholds(updated)
                await loadHousehold(updated.find((item) => item.id === household.id))
                setInvitation(null)
                setFeedback({ text: 'Household ownership transferred.' })
              })}
              onAddress={(address) => run(async () => {
                const saved = await api.createAddress(address)
                setAddresses((current) => [...current, saved])
                setFeedback({ text: 'Address shared.' })
                return saved
              })}
              onUpdatePin={(id, pin) => updatePin(api, setAddresses, id, pin)}
              onDeleteAddress={(id) => run(async () => { await api.deleteAddress(id); setAddresses(await api.addresses()) })}
              onOpenCart={(id) => run(() => openCart(id))}
              onOpenLists={() => openPage('lists')}
              onShopCart={(id) => run(() => selectShoppingCart(id))}
              onCreateCart={(name, adultsOnly) => run(async () => {
                const created = await api.create(name, adultsOnly)
                setCarts(await api.carts())
                await selectShoppingCart(created.id)
                setFeedback({ text: `${created.name} created.` })
              })}
              onDeleteCart={(id) => run(async () => {
                await api.deleteCart(id)
                const remaining = await api.carts()
                setCarts(remaining)
                if (shoppingCartId === id) {
                  const next = remaining.find((cart) => !cart.adults_only) ?? remaining[0]
                  setShoppingCartId(next?.id ?? null)
                  if (next) {
                    const cart = await api.cart(next.id)
                    setQuantities(sharedCartQuantities(cart.contributions))
                  } else setQuantities({})
                }
                setFeedback({ text: 'Cart deleted.' })
              })} /> :
            <GroceryPage products={products} busy={busy} quantities={displayQuantities}
              canEditCart={!household || carts.find((cart) => cart.id === shoppingCartId)?.can_edit_items !== false}
              canOpenCart={Object.values(displayQuantities).some((quantity) => quantity > 0)}
              householdCarts={household ? carts : []} activeCartId={shoppingCartId}
              onSelectCart={(id) => run(() => selectShoppingCart(id))}
              onAdjust={queueAdjust}
              onOpenCart={() => {
                const cart = carts.find((candidate) => candidate.id === shoppingCartId)
                if (cart) run(() => openCart(cart.id))
                else setView('personal-cart')
              }} />}
      </main>
      {authOpen && !restoringSession && !session && <GoogleAuthModal busy={busy} onClose={() => setAuthOpen(false)}
        onAuthenticated={(signedIn) => run(async () => {
          await hydrateSession(signedIn); setAuthOpen(false); setFeedback({ text: 'Welcome back.' })
        })} />}
    </>
  )
}

createRoot(document.getElementById('root')).render(<App />)
