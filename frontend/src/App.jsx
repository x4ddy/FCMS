import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { connectCarts, householdApi, signIn } from './api.js'
import { CartDetail } from './CartDetail.jsx'
import { applyCartSnapshot, applyConfirmedEdit } from './cartState.js'
import './styles.css'

function SignIn({ busy, onSignIn }) {
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')

  function fillDemo(identifier, password) {
    setIdentifier(identifier)
    setPassword(password)
  }

  function submit(event) {
    event.preventDefault()
    onSignIn(identifier, password)
  }

  return (
    <section className="panel sign-in">
      <h1>Sign in</h1>
      <div className="demo-buttons">
        <button className="secondary" disabled={busy}
          onClick={() => fillDemo('primary@example.test', 'primary-demo-2026')}>Use primary demo</button>
        <button className="secondary" disabled={busy}
          onClick={() => fillDemo('demo-child', 'child-demo-2026')}>Use child demo</button>
        <button className="secondary" disabled={busy}
          onClick={() => fillDemo('adult@example.test', 'adult-demo-2026')}>Use adult demo</button>
      </div>
      <form onSubmit={submit}>
        <label htmlFor="identifier">Email or username</label>
        <input id="identifier" autoComplete="username" required maxLength={254} disabled={busy}
          value={identifier} onChange={(event) => setIdentifier(event.target.value)} />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required maxLength={256} disabled={busy}
          value={password} onChange={(event) => setPassword(event.target.value)} />
        <button disabled={busy}>Sign in</button>
      </form>
    </section>
  )
}

function CartList({ carts, busy, canCreateAdultsOnlyCart, onRefresh, onCreate, onOpen }) {
  const [name, setName] = useState('')

  function createCart(event) {
    event.preventDefault()
    const adultsOnly = new FormData(event.currentTarget).get('cart-type') === 'adults-only'
    if (name.trim()) onCreate(name.trim(), adultsOnly)
  }

  return (
    <>
      <div className="page-heading">
        <h1>Household carts</h1>
        <button className="secondary" onClick={onRefresh} disabled={busy}>Refresh carts</button>
      </div>
      <form className="panel create-form" onSubmit={createCart}>
        <label htmlFor="cart-name">New cart name</label>
        <div className="field-action">
          <input id="cart-name" placeholder="School snacks" required maxLength={100} disabled={busy}
            value={name} onChange={(event) => setName(event.target.value)} />
          <button disabled={busy}>Create cart</button>
        </div>
        {canCreateAdultsOnlyCart && (
          <div className="cart-type">
            <label htmlFor="cart-type">Cart type</label>
            <select id="cart-type" name="cart-type" defaultValue="regular" disabled={busy}>
              <option value="regular">Regular</option>
              <option value="adults-only">Adults only</option>
            </select>
          </div>
        )}
      </form>
      {carts.length === 0 ? <p className="empty">You don’t have any visible carts yet.</p> : (
        <ul className="cart-list">
          {carts.map((cart) => (
            <li key={cart.id}>
              <button className="cart-tile" disabled={busy} onClick={() => onOpen(cart.id)}>
                <strong>{cart.name}</strong>
                <span>{cart.adults_only && 'Adults only · '}{cart.can_edit_items ? 'Can edit' : 'View access'}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function MembersPanel({ members, busy, onPromote }) {
  return (
    <section className="panel household-members">
      <h2>Household members</h2>
      <ul className="rows">
        {members.map((member) => (
          <li key={member.id}>
            <div>
              <strong>{member.display_name}</strong>
              <small>{member.role === 'primary' ? 'Primary' : member.is_adult ? 'Adult member' : 'Member'}</small>
            </div>
            {member.is_adult && member.role === 'member' && (
              <button className="secondary" disabled={busy}
                onClick={() => {
                  if (window.confirm(`Make ${member.display_name} a household primary?`)) onPromote(member.id)
                }}>
                Make primary
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

function App() {
  const [session, setSession] = useState(null)
  const [carts, setCarts] = useState([])
  const [products, setProducts] = useState([])
  const [members, setMembers] = useState([])
  const [detail, setDetail] = useState(null)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const [connected, setConnected] = useState(false)
  const token = session?.access_token
  const cartId = detail?.cart.id
  const api = token ? householdApi(token) : null

  function signOut() {
    setSession(null)
    setCarts([])
    setProducts([])
    setMembers([])
    setDetail(null)
    setFeedback(null)
  }

  useEffect(() => {
    setConnected(false)
    if (!token) return
    let stopped = false
    let reconnect
    let socket

    function connect() {
      socket = connectCarts(token, cartId)
      socket.onmessage = (event) => {
        if (stopped) return
        const snapshot = JSON.parse(event.data)
        setConnected(true)
        setCarts(snapshot.carts)
        if (typeof snapshot.can_create_adults_only_cart === 'boolean') {
          setSession((current) => current && {
            ...current, can_create_adults_only_cart: snapshot.can_create_adults_only_cart,
          })
        }
        if (snapshot.members) setMembers(snapshot.members)
        if (snapshot.cart === null && cartId) {
          setDetail(null)
          setFeedback({ error: true, text: 'This cart is no longer available to you.' })
        } else if (snapshot.cart) {
          setDetail((current) => {
            const next = applyCartSnapshot(current, snapshot.cart, snapshot.revision)
            return next !== current && snapshot.grants ? { ...next, grants: snapshot.grants } : next
          })
        }
      }
      socket.onclose = ({ code }) => {
        if (stopped) return
        setConnected(false)
        if (code === 4401 || code === 4404) {
          signOut()
          setFeedback({ error: true, text: code === 4401
            ? 'Your session ended. Sign in again.' : 'You no longer have access to this household.' })
          return
        }
        reconnect = window.setTimeout(connect, 1500)
      }
    }

    connect()
    return () => {
      stopped = true
      window.clearTimeout(reconnect)
      socket.close()
    }
  }, [token, cartId])

  async function run(action) {
    setBusy(true)
    setFeedback(null)
    try {
      await action()
    } catch (error) {
      if (error.status === 401 && session) {
        signOut()
        setFeedback({ error: true, text: 'Your session ended. Sign in again.' })
      } else {
        setFeedback({ error: true, text: error.message })
      }
    } finally {
      setBusy(false)
    }
  }

  async function openCart(id) {
    const cart = await api.cart(id)
    const grants = cart.can_manage_access ? await api.grants(id) : []
    setDetail({ cart, grants, snapshotRevision: 0, pendingEdits: new Map() })
  }

  async function showCarts() {
    setDetail(null)
    const [visibleCarts, capabilities] = await Promise.all([api.carts(), api.capabilities()])
    setCarts(visibleCarts)
    setSession((current) => current && { ...current, ...capabilities })
    setMembers(capabilities.can_create_adults_only_cart ? await api.members() : [])
  }

  async function updateItem(save) {
    try {
      const edit = await save()
      setDetail((current) => applyConfirmedEdit(current, cartId, edit))
      setFeedback({ text: edit.quantity ? 'Quantity saved.' : 'Item removed.' })
    } catch (error) {
      if (error.status !== 404 || !['Cart not found', 'Household not found'].includes(error.message)) throw error
      await showCarts()
      setFeedback({ error: true, text: 'Access changed. This cart is no longer visible to you.' })
    }
  }

  async function deleteCart() {
    await api.deleteCart(cartId)
    await showCarts()
    setFeedback({ text: 'List deleted.' })
  }

  async function promoteMember(memberId) {
    await api.promoteMember(memberId)
    setMembers(await api.members())
    setFeedback({ text: 'Primary role granted.' })
  }

  async function changeGrant(member, grant) {
    await api.changeGrant(cartId, member, grant)
    const grants = await api.grants(cartId)
    setDetail((current) => current?.cart.id === cartId ? { ...current, grants } : current)
    setFeedback({ text: grant ? 'Access granted.' : 'Access revoked.' })
  }

  return (
    <>
      <header className="site-header">
        <div className="shell header-content">
          <span className="brand">FamilyCart</span>
          {session && <div className="account">
            <span>{session.user.display_name}</span>
            <button className="secondary" onClick={signOut} disabled={busy}>Sign out</button>
          </div>}
        </div>
      </header>
      <main className={`shell ${!session ? 'signed-out' : ''}`}>
        {session && detail && (
          <button className="secondary back" onClick={() => run(showCarts)} disabled={busy}>Back to carts</button>
        )}
        {session && !connected && <p className="muted" role="status">Connecting to live cart sync…</p>}
        {feedback && <p className={feedback.error ? 'notice error' : 'notice'}
          role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>}
        {!session ? (
          <SignIn busy={busy} onSignIn={(identifier, password) => run(async () => {
            const signedIn = await signIn(identifier, password)
            const signedInApi = householdApi(signedIn.access_token)
            const [visibleCarts, visibleProducts, capabilities] = await Promise.all([
              signedInApi.carts(), signedInApi.products(), signedInApi.capabilities(),
            ])
            setCarts(visibleCarts)
            setProducts(visibleProducts)
            if (capabilities.can_create_adults_only_cart) setMembers(await signedInApi.members())
            setSession({ ...signedIn, ...capabilities })
          })} />
        ) : detail ? (
          <CartDetail key={detail.cart.id} {...detail} user={session.user} busy={busy} products={products} members={members}
            onSetQuantity={(sku, quantity) => run(() => updateItem(() => api.setQuantity(cartId, sku, quantity)))}
            onAdjustQuantity={(sku, delta) => run(() => updateItem(() => api.adjustQuantity(cartId, sku, delta)))}
            onRemoveItem={(sku, contributor) => run(() => updateItem(() => api.removeContribution(cartId, sku, contributor)))}
            onChangeGrant={(member, grant) => run(() => changeGrant(member, grant))}
            onDeleteCart={() => run(deleteCart)} />
        ) : (
          <>
            <CartList carts={carts} busy={busy} canCreateAdultsOnlyCart={session.can_create_adults_only_cart}
              onRefresh={() => run(showCarts)} onOpen={(id) => run(() => openCart(id))}
              onCreate={(name, adultsOnly) => run(async () => {
                const cart = await api.create(name, adultsOnly)
                await openCart(cart.id)
              })} />
            {session.can_create_adults_only_cart && (
              <MembersPanel members={members} busy={busy} onPromote={(id) => run(() => promoteMember(id))} />
            )}
          </>
        )}
      </main>
    </>
  )
}

createRoot(document.getElementById('root')).render(<App />)
