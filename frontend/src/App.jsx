import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AddressBook } from './AddressBook.jsx'
import {
  accountApi, connectCarts, householdApi, logoutSession, onAccessTokenChanged, refreshAccessToken, restoreSession,
} from './api.js'
import { DateOfBirthForm, GoogleAuthModal } from './Auth.jsx'
import { CartView } from './CartDrawer.jsx'
import { applyAdjustment, cartActivity, cartDifferences, cartQuantities, cartRows, combined, withTaps } from './cartState.js'
import { Checkout, OrderConfirmation } from './Checkout.jsx'
import { isUnconfirmed, readAttempt, saveAttempt } from './checkoutState.js'
import { AddressesPage, HouseholdPage } from './Household.jsx'
import { OrderHistory } from './OrderHistory.jsx'
import { Landing, ShopPage } from './Shop.jsx'
import { ListsPage } from './ShoppingLists.jsx'
import { Avatar, confirmAction, ConfirmHost, Empty, Icon, money, Overlay } from './ui.jsx'
import './styles.css'

const clock = new Intl.DateTimeFormat('en-AE', { timeStyle: 'short' })

function orderFromLocation() {
  return window.location.pathname.match(/^\/orders\/([a-z0-9-]+)\/?$/i)?.[1] ?? null
}

function orderHistoryFromLocation() {
  return /^\/orders\/?$/.test(window.location.pathname)
}

/** What other members did while this page was open. */
function ActivityList({ activity, onClear }) {
  return <div className="drawer-body">
    {activity.length === 0 ? <Empty icon="🔔" title="Nothing new">
      <p>When someone in your household changes the cart or places an order, it shows up here.</p>
    </Empty> : <>
      <button className="text-button" onClick={onClear}>Clear</button>
      <ul className="activity-list">{activity.map((item) => <li key={item.id}>
        <Icon name="cart" size={18} /><span>{item.text}<small className="muted">{clock.format(item.at)}</small></span></li>)}</ul>
    </>}
  </div>
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
  const [invitation, setInvitation] = useState(null)
  // The cart being shopped. The catalogue and the cart drawer show the same one.
  const [cartId, setCartId] = useState(null)
  const [cart, setCart] = useState(null)
  const [grants, setGrants] = useState([])
  const [picked, setSelected] = useState(() => new Set())
  // "Select all" also covers items that arrive afterwards, until something is unticked.
  const [allSelected, setAllSelected] = useState(false)
  // sku -> change. Taps wait a moment and are sent together; sending holds those on their way.
  const [taps, setTaps] = useState({})
  const [sending, setSending] = useState({})
  const [live, setLive] = useState(false)
  const [drawer, setDrawer] = useState(null)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [notices, setNotices] = useState([])
  const [activity, setActivity] = useState([])
  const [unread, setUnread] = useState(0)
  const [checkoutCart, setCheckoutCart] = useState(null)
  const [checkoutChanges, setCheckoutChanges] = useState([])
  const [memberRequests, setMemberRequests] = useState([])
  const [pendingOrder, setPendingOrder] = useState(null)
  const [orderId, setOrderId] = useState(orderFromLocation)
  const [showOrderHistory, setShowOrderHistory] = useState(orderHistoryFromLocation)
  const [confirmation, setConfirmation] = useState(null)
  const [orderError, setOrderError] = useState(null)
  const noticeSequence = useRef(0)
  const productNames = useRef(new Map())
  const currentUser = useRef(null)
  const searchInput = useRef(null)
  const requestIds = useRef(null)
  const leavingRef = useRef(null)
  // { api, cartId } while this person is checking out.
  const checkoutSession = useRef(null)
  const drawerRef = useRef(null)
  drawerRef.current = drawer
  const cartIdRef = useRef(null)
  cartIdRef.current = cartId
  // sku -> { cartId, product, delta, timer }
  const waiting = useRef(new Map())
  const requests = useRef(Promise.resolve())
  // Live updates older than our own last saved edit are ignored.
  const editRevision = useRef(0)
  const token = session?.access_token
  const userId = session?.user.id
  const needsAgeSetup = session?.user.age_setup_required
  const householdId = household?.id
  const isPrimary = household?.role === 'primary'  // the admin
  const account = useMemo(() => token ? accountApi(token) : null, [token])
  const api = useMemo(() => token && householdId ? householdApi(token, householdId) : null, [token, householdId])

  useEffect(() => {
    productNames.current = new Map(products.map((product) => [product.sku, product.name]))
  }, [products])

  const showPopup = useCallback((text, kind = 'info') => {
    const notice = { id: ++noticeSequence.current, text, kind, at: new Date() }
    setNotices((current) => [...current, notice].slice(-3))
    if (kind === 'activity') {
      setActivity((current) => [notice, ...current].slice(0, 50))
      if (drawerRef.current !== 'activity') setUnread((count) => count + 1)
    }
    window.setTimeout(() => setNotices((current) => current.filter((item) => item.id !== notice.id)), kind === 'error' ? 8000 : 5000)
  }, [])

  function forgetCart() {
    for (const entry of waiting.current.values()) window.clearTimeout(entry.timer)
    waiting.current.clear()
    editRevision.current = 0
    setTaps({}); setSending({}); setCart(null); setGrants([]); setSelected(new Set()); setAllSelected(false)
  }

  function clearSession() {
    currentUser.current = null
    forgetCart()
    setSession(null); setHousehold(null); setHouseholds([]); setCarts([]); setProducts([]); setMembers([])
    setAddresses([]); setInvitation(null); setCartId(null); setCheckoutCart(null); setPendingOrder(null); setMemberRequests([])
    setConfirmation(null); setOrderError(null); setNotices([]); setActivity([]); setUnread(0)
    setDrawer(null); setQuery(''); setView('shop')
  }

  useEffect(() => onAccessTokenChanged((nextToken, refreshedUser) => {
    if (!nextToken) clearSession()
    else if (refreshedUser && currentUser.current && refreshedUser.id !== currentUser.current) clearSession()
    else setSession((current) => current && {
      ...current, access_token: nextToken,
      user: refreshedUser ? { ...current.user, is_adult: refreshedUser.is_adult } : current.user,
    })
  }), [])

  async function loadHousehold(chosen, accessToken = token) {
    if (!chosen) {
      forgetCart()
      setHousehold(null); setCarts([]); setMembers([]); setAddresses([]); setCartId(null); setMemberRequests([])
      setProducts(await accountApi(accessToken).products())
      setSession((current) => current && { ...current, is_adult: current.user.is_adult, age_status: undefined })
      return
    }
    const selectedApi = householdApi(accessToken, chosen.id)
    const [visibleCarts, capabilities, savedAddresses, visibleProducts, visibleMembers, requests] = await Promise.all([
      selectedApi.carts(), selectedApi.capabilities(), selectedApi.addresses(), selectedApi.products(),
      chosen.role === 'primary' ? selectedApi.members() : [], selectedApi.memberRequests(),
    ])
    const initial = visibleCarts.find((item) => !item.adults_only) ?? visibleCarts[0]
    const initialCart = initial ? await selectedApi.cart(initial.id) : null
    forgetCart()
    setProducts(visibleProducts); setHousehold(chosen); setCarts(visibleCarts); setMembers(visibleMembers)
    setAddresses(savedAddresses); setMemberRequests(requests); setInvitation(null); setCartId(initial?.id ?? null); setCart(initialCart)
    setSession((current) => current && { ...current, ...capabilities })
  }

  async function hydrateSession(signedIn) {
    if (signedIn.user.age_setup_required) {
      currentUser.current = signedIn.user.id
      setSession(signedIn)
      return
    }
    const signedInAccount = accountApi(signedIn.access_token)
    const visibleHouseholds = await signedInAccount.households()
    currentUser.current = signedIn.user.id
    setSession(signedIn)
    setHouseholds(visibleHouseholds)
    setPendingOrder(readAttempt(signedIn.user.id))
    await loadHousehold(visibleHouseholds[0] ?? null, signedIn.access_token)
  }

  useEffect(() => {
    let cancelled = false
    restoreSession()
      .then((signedIn) => cancelled ? undefined : hydrateSession(signedIn))
      .catch((error) => {
        if (cancelled) return
        clearSession()
        if (error.status !== 401) showPopup('Your session could not be restored right now. Reload the page to try again.', 'error')
      })
      .finally(() => { if (!cancelled) setRestoringSession(false) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!token || !householdId) return
    let stopped = false
    let reconnect
    let socket
    let previous = null
    let waitingBefore = 0
    let cartsBefore = null
    function connect() {
      editRevision.current = 0  // the first message is read fresh from the database
      socket = connectCarts(token, householdId, cartId)
      socket.onmessage = (event) => {
        if (stopped) return
        const snapshot = JSON.parse(event.data)
        setLive(true)
        setCarts(snapshot.carts)
        if (snapshot.household_role) {
          setHousehold((current) => current && { ...current, role: snapshot.household_role })
          setHouseholds((current) => current.map((item) => item.id === householdId
            ? { ...item, role: snapshot.household_role } : item))
          setSession((current) => current && {
            ...current,
            can_create_adults_only_cart: snapshot.can_create_adults_only_cart,
            is_adult: snapshot.is_adult, age_status: snapshot.age_status,
          })
        }
        if (snapshot.members) setMembers((current) => snapshot.members.map((member) => ({
          // Claimed ages come from the member list, not from live updates.
          ...member, claimed_age: current.find((other) => other.id === member.id)?.claimed_age,
        })))
        if (snapshot.addresses) setAddresses(snapshot.addresses)
        if (snapshot.member_requests) {
          const known = requestIds.current
          for (const item of snapshot.member_requests) {
            if (known && snapshot.household_role === 'primary' && item.requested_by !== userId && !known.has(item.id)) {
              const asker = snapshot.members?.find((member) => member.id === item.requested_by)?.display_name ?? 'A member'
              showPopup(`${asker} asks to add ${item.name} to the household.`, 'activity')
            }
            if (known && item.requested_by === userId && item.status === 'APPROVED' && known.get(item.id) === 'PENDING') {
              showPopup(`Your request to add ${item.name} was approved.`, 'activity')
            }
          }
          requestIds.current = new Map(snapshot.member_requests.map((item) => [item.id, item.status]))
          setMemberRequests(snapshot.member_requests)
        }
        if (snapshot.cart === null && cartId && snapshot.next_cart_id) {
          // Checked out. What was added meanwhile is in the new cart that replaced it.
          const buyer = snapshot.members?.find((member) => member.id === snapshot.ordered_by)?.display_name
          if (snapshot.ordered_by !== userId) showPopup(`${buyer ?? 'The cart'} ${buyer ? 'placed an order' : 'was checked out'}. New items are in the next cart.`, 'activity')
          selectCart(snapshot.next_cart_id)
        } else if (snapshot.cart === null && cartId) {
          previous = null
          forgetCart()
          setCartId(null)
          setCheckoutCart(null)
          if (drawerRef.current === 'checkout') setDrawer('cart')
          if (snapshot.access_notice) showPopup(snapshot.access_notice, 'error')
        } else if (snapshot.cart) {
          if (snapshot.revision < editRevision.current) return
          if (previous !== null) {
            const memberNames = new Map((snapshot.members ?? []).map((member) => [member.id, member.display_name]))
            for (const message of cartActivity(previous, combined(snapshot.cart), userId, snapshot.actor_id,
              memberNames, productNames.current, snapshot.actor_ids, snapshot.ordered_by)) {
              showPopup(`${message} ${message.endsWith('placed an order') ? 'from' : message.includes(' added ') && snapshot.cart.checkout ? 'for the cart after' : 'in'} ${snapshot.cart.name}.`, 'activity')
            }
          }
          const started = snapshot.carts.find((item) => cartsBefore && !cartsBefore.has(item.id))
          if (snapshot.ordered_by && snapshot.ordered_by !== userId && waitingBefore > 0 && started) {
            showPopup(`Items added during checkout are in ${started.name}.`, 'activity')
          }
          waitingBefore = snapshot.cart.waiting?.length ?? 0
          previous = combined(snapshot.cart)
          setCart(snapshot.cart)
          if (snapshot.grants) setGrants(snapshot.grants)
        }
        cartsBefore = new Set(snapshot.carts.map((item) => item.id))
      }
      socket.onclose = async ({ code, reason }) => {
        setLive(false)
        if (stopped) return
        if (code === 4401) {
          // Access tokens expire after 10 minutes. Refreshing publishes the new
          // token, which causes this effect to reconnect the socket securely.
          try { await refreshAccessToken() } catch (error) {
            // On 401 the token listener clears the session; otherwise try again.
            if (error.status !== 401 && !stopped) reconnect = window.setTimeout(connect, 2000)
          }
        } else if (code === 4404 && leavingRef.current === householdId) {
          // This person just left or deleted it; they already know.
        } else if (code === 4404) {
          setHouseholds((current) => current.filter((item) => item.id !== householdId))
          checkoutSession.current = null
          setCheckoutCart(null); setDrawer(null); setView('shop')
          loadHousehold(null).catch(() => {})
          showPopup(reason || 'Your household access was revoked. Contact the household admin.', 'error')
        } else reconnect = window.setTimeout(connect, 1500)
      }
    }
    connect()
    return () => { stopped = true; window.clearTimeout(reconnect); socket?.close() }
  }, [token, householdId, cartId])

  // Always follow a cart that still exists. If the cart being shopped is deleted
  // or access to it is removed, fall back to the household's regular shared cart.
  useEffect(() => {
    if (!householdId || carts.length === 0 || carts.some((item) => item.id === cartId)) return
    selectCart((carts.find((item) => !item.adults_only) ?? carts[0]).id)
  }, [householdId, carts, cartId])

  // Which products may be seen can change when the primary decides on an age.
  const firstAge = useRef(true)
  useEffect(() => {
    if (!api || session?.is_adult === undefined) return
    if (firstAge.current) { firstAge.current = false; return }  // just loaded
    let cancelled = false
    api.products().then((visible) => { if (!cancelled) setProducts(visible) }).catch(() => {})
    return () => { cancelled = true }
  }, [api, session?.is_adult])

  // Ask for claimed ages when someone new is waiting for a decision.
  const unknownAges = !isPrimary ? '' : members
    .filter((member) => member.age_status === 'PENDING' && member.claimed_age === undefined).map((member) => member.id).join()
  useEffect(() => {
    if (!api || !unknownAges) return
    api.members().then((listed) => setMembers(listed.map((member) => ({ ...member, claimed_age: member.claimed_age ?? null }))))
      .catch(() => {})
  }, [api, unknownAges])

  // Keep the checkout open while its drawer is; a closed tab frees the cart by itself.
  const checkingOut = drawer === 'checkout' && Boolean(checkoutSession.current)
  useEffect(() => {
    if (!checkingOut) return
    const renew = window.setInterval(() => {
      const session = checkoutSession.current
      session?.api.startCheckout(session.cartId).catch((error) => {
        if (!error.status || error.status >= 500 || error.status === 401) return
        checkoutSession.current = null
        setCheckoutCart(null)
        setDrawer((open) => open === 'checkout' ? 'cart' : open)
        showPopup(error.message, 'error')
      })
    }, 60000)
    const leave = () => endCheckout()
    window.addEventListener('pagehide', leave)
    return () => { window.clearInterval(renew); window.removeEventListener('pagehide', leave) }
  }, [checkingOut])

  // Closing checkout in any way ends it, unless the order was placed.
  useEffect(() => { if (drawer !== 'checkout') endCheckout() }, [drawer])

  // If somebody left checkout without saying so, reading the cart gives back what was waiting.
  const otherCheckout = cart?.checkout && cart.checkout.user_id !== userId ? cart.checkout.expires_at : null
  useEffect(() => {
    if (!otherCheckout || !cartId) return
    const timer = window.setTimeout(() => reloadCart(cartId), Math.max(1000, new Date(otherCheckout) - Date.now() + 1500))
    return () => window.clearTimeout(timer)
  }, [otherCheckout, cartId])

  // Who has access is only needed, and only visible, when the cart is open.
  const canManageAccess = Boolean(cart?.can_manage_access)
  useEffect(() => {
    if (!api || !cartId || drawer !== 'cart' || !canManageAccess) return
    let cancelled = false
    api.grants(cartId).then((listed) => { if (!cancelled) setGrants(listed) }).catch(() => {})
    return () => { cancelled = true }
  }, [api, cartId, drawer, canManageAccess])

  useEffect(() => {
    function handlePopState() {
      setOrderId(orderFromLocation())
      setShowOrderHistory(orderHistoryFromLocation())
      setCheckoutCart(null); setDrawer(null); setConfirmation(null); setOrderError(null)
    }
    function handleKey(event) {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)
      if (event.key === '/' && !typing && !drawerRef.current) { event.preventDefault(); searchInput.current?.focus() }
    }
    window.addEventListener('popstate', handlePopState)
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('popstate', handlePopState)
      window.removeEventListener('keydown', handleKey)
    }
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
    setCheckoutCart(null)
    setDrawer(null)
    if (next !== 'shop') setQuery('')
    setView(next)
    window.scrollTo({ top: 0 })
  }

  function openOrder(id) {
    setCheckoutCart(null)
    setDrawer(null)
    setOrderId(id)
    setShowOrderHistory(false)
    setConfirmation(null)
    setOrderError(null)
    window.history.pushState(null, '', `/orders/${id}`)
    window.scrollTo({ top: 0 })
  }

  function resolveOrder(id) {
    openOrder(id)
    saveAttempt(userId, null)
    setPendingOrder(null)
  }

  async function submitOrder(attempt) {
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
    checkoutSession.current = null  // finished, not abandoned
    resolveOrder(placed.order_id)
    setConfirmation(placed)
    setSelected(new Set())
    if (attempt.cart.household_id !== householdId) return
    const next = placed.next_cart
    if (!next) { reloadCart(attempt.cart.cart_id); return }
    showPopup(`Items added during checkout are in ${next.replaces ? 'a new cart' : next.name}.`)
    api.carts().then((listed) => { setCarts(listed); selectCart(next.id) }).catch(() => {})
  }

  async function run(action) {
    setBusy(true)
    try { return await action() }
    catch (error) {
      if (error.status === 401) clearSession()
      showPopup(error.message, 'error')
    } finally { setBusy(false) }
  }

  /** Put the screen in line with what is saved. The next live update also does this. */
  async function reloadCart(id) {
    try {
      const fresh = await api.cart(id)
      if (cartIdRef.current === id && fresh.id === id) setCart(fresh)
      return fresh
    } catch { /* the next live update will correct it */ }
  }

  function selectCart(id) {
    if (id === cartIdRef.current) return
    flushTaps()
    setCart(null); setGrants([]); setSelected(new Set()); setAllSelected(false)
    setTaps({}); setSending({})
    editRevision.current = 0
    setCartId(id)
    cartIdRef.current = id
    reloadCart(id)
  }

  // While somebody checks out, the cart stands still and changes go to the items waiting for the next one.
  const locked = Boolean(cart?.checkout)
  const changing = locked ? cart.waiting : cart?.contributions
  const changingTotals = useMemo(() => withTaps(cartQuantities(changing), sending, taps), [changing, sending, taps])
  const own = useMemo(() => withTaps(cartQuantities(changing?.filter((row) => row.contributor_id === userId)), sending, taps),
    [changing, sending, taps, userId])
  const inCart = useMemo(() => locked ? cartQuantities(cart.contributions) : changingTotals, [locked, cart, changingTotals])
  const quantities = useMemo(() => locked ? withTaps(inCart, changingTotals) : inCart, [locked, inCart, changingTotals])
  const rows = useMemo(() => cartRows(cart?.contributions ?? [], inCart, products), [cart, inCart, products])
  const waitingRows = useMemo(() => locked ? cartRows(cart.waiting, changingTotals, products) : [], [locked, cart, changingTotals, products])
  const bySku = useMemo(() => new Map(products.map((product) => [product.sku, product])), [products])
  const names = useMemo(() => new Map([...members.map((member) => [member.id, member.display_name]), [userId, session?.user.display_name]]),
    [members, userId, session?.user.display_name])

  const addTo = (change, sku, delta) => {
    const next = { ...change, [sku]: (change[sku] ?? 0) + delta }
    if (next[sku] === 0) delete next[sku]
    return next
  }

  /** Show a tap at once; send all taps on the same product as one request after a short pause. */
  const adjustState = useRef(null)
  const adjust = useCallback((product, delta) => adjustState.current(product, delta), [])
  adjustState.current = (product, delta) => {
    const { sku } = product
    const entry = waiting.current.get(sku) ?? { cartId, product, delta: 0 }
    const limited = Math.max(-99, Math.min(99, entry.delta + delta))
    const applied = limited - entry.delta
    if (!applied || (changingTotals[sku] ?? 0) + applied < 0) return
    entry.delta = limited
    setTaps((current) => addTo(current, sku, applied))
    window.clearTimeout(entry.timer)
    entry.timer = window.setTimeout(() => flushTaps(sku), 400)
    waiting.current.set(sku, entry)
  }

  /** Send waiting taps now. Resolves when everything sent so far is saved. */
  function flushTaps(only) {
    for (const [sku, entry] of [...waiting.current]) {
      if (only && sku !== only) continue
      window.clearTimeout(entry.timer)
      waiting.current.delete(sku)
      if (entry.delta === 0) continue
      setTaps((current) => { const next = { ...current }; delete next[sku]; return next })
      setSending((current) => addTo(current, sku, entry.delta))
      // One after another, so two changes to the same product cannot overtake each other.
      requests.current = requests.current.then(() => sendTap(sku, entry))
    }
    return requests.current
  }

  async function sendTap(sku, { cartId: target, delta }) {
    let current = target
    const followed = () => cartIdRef.current === current
    try {
      if (!current) {
        const created = await api.create('Shared shopping cart', false)
        current = created.id
        cartIdRef.current = created.id
        setCarts(await api.carts())
        setCartId(created.id)
        setCart({ ...created, contributions: created.contributions ?? [] })
      }
      const update = await api.adjustSharedQuantity(current, sku, delta)
      if (!followed()) return
      editRevision.current = Math.max(editRevision.current, update.revision ?? 0)
      let matches = true
      setCart((shown) => {
        if (!shown) return shown
        const part = update.waiting ? 'waiting' : 'contributions'
        const next = { ...shown, [part]: applyAdjustment(shown[part] ?? [], sku, delta, currentUser.current) }
        // A checkout may have started or ended since the last update.
        matches = (cartQuantities(combined(next))[sku] ?? 0) === update.quantity && Boolean(shown.checkout) === update.waiting
        return next
      })
      setSending((change) => sku in change ? addTo(change, sku, -delta) : change)
      // Someone else changed the same product meanwhile.
      window.setTimeout(() => { if (!matches && followed()) reloadCart(current) }, 0)
    } catch (error) {
      if (followed()) setSending((change) => sku in change ? addTo(change, sku, -delta) : change)
      if (error.status === 401) { clearSession(); return }
      showPopup(error.message, 'error')
      if (followed() && current) reloadCart(current)
    }
  }

  async function removeItem(row) {
    if (!await confirmAction({
      title: `Remove ${row.product?.name ?? row.sku}?`,
      text: row.people.some((item) => item.contributor_id !== userId) ? 'Other members added this too. It is removed for everyone.' : undefined,
      action: 'Remove', danger: true,
    })) return
    const id = cartId
    await flushTaps()
    const part = locked ? 'waiting' : 'contributions'
    setCart((shown) => shown && { ...shown, [part]: shown[part].filter((item) => item.sku !== row.sku) })
    try {
      const update = await api.removeSharedItem(id, row.sku)
      if (cartIdRef.current === id) editRevision.current = Math.max(editRevision.current, update.revision ?? 0)
    } catch (error) {
      if (error.status === 401) { clearSession(); return }
      showPopup(error.message, 'error')
      reloadCart(id)
    }
  }

  function updatePin(id, pin) {
    return run(async () => {
      const saved = await api.updateAddressPin(id, pin)
      setAddresses((current) => current.map((address) => address.id === id ? saved : address))
      return saved
    })
  }

  const addressBook = {
    api, busy, addresses, displayName: session?.user.display_name,
    canEdit: (address) => isPrimary || address.created_by === userId,
    onSave: (address) => run(async () => {
      const saved = await api.createAddress(address)
      setAddresses((current) => [...current.filter((other) => other.id !== saved.id), saved])
      showPopup('Address added.')
      return saved
    }),
    onUpdatePin: updatePin,
    onDelete: (id) => run(async () => {
      await api.deleteAddress(id)
      setAddresses(await api.addresses())
      showPopup('Address removed.')
    }),
  }

  async function refreshMembers() {
    if (isPrimary) setMembers(await api.members())
  }

  /** Leave or delete the household being used, then continue with another one if there is one. */
  function endMembership(request, message) {
    return run(async () => {
      await flushTaps()
      await request()
      leavingRef.current = householdId  // the live connection will report that access ended
      const remaining = await account.households()
      setHouseholds(remaining)
      await loadHousehold(remaining[0] ?? null)
      openPage('household')
      showPopup(message)
    })
  }

  function openDrawer(next) {
    if (next === 'activity') setUnread(0)
    setDrawer(next)
  }

  /** Save what is waiting, bring the cart up to date, and only then open checkout. */
  async function startCheckout() {
    await run(async () => {
      await flushTaps()
      const id = cartIdRef.current
      const everything = available.every((row) => selected.has(row.sku))
      const fresh = await api.startCheckout(id)
      checkoutSession.current = { api, cartId: id }
      if (cartIdRef.current !== id) { endCheckout(); return }
      const listed = new Set(fresh.contributions.map((row) => row.sku))
      const chosen = new Set([...(everything ? listed : selected)].filter((sku) => listed.has(sku) && bySku.has(sku)))
      setCheckoutChanges(cartDifferences(cart.contributions, fresh.contributions, productNames.current))
      setCart(fresh)
      setAllSelected(false)  // what is being ordered no longer changes
      setSelected(chosen)
      if (chosen.size === 0) {
        endCheckout()
        showPopup('The items you selected are no longer in the cart.', 'error')
        return
      }
      setCheckoutCart({ kind: 'household', household_id: householdId, cart_id: id })
      setDrawer('checkout')
    })
  }

  function endCheckout() {
    const session = checkoutSession.current
    if (!session) return
    checkoutSession.current = null
    session.api.leaveCheckout(session.cartId).then(() => {
      if (cartIdRef.current === session.cartId) return session.api.cart(session.cartId)
    }).then((fresh) => {
      if (fresh && cartIdRef.current === fresh.id) setCart(fresh)
    }).catch(() => { /* it runs out by itself within a few minutes */ })
  }

  const ownAddress = addresses.find((address) => address.created_by === userId)
  const needsOwnAddress = household && !ownAddress
  const available = rows.filter((row) => row.product)
  const selected = allSelected ? new Set(available.map((row) => row.sku)) : picked
  const itemCount = [...rows, ...waitingRows].reduce((total, row) => total + row.quantity, 0)
  const cartTotal = [...rows, ...waitingRows].reduce((total, row) => total + (row.product?.price_fils ?? 0) * row.quantity, 0)
  const checkoutAddresses = addresses.map((address) => ({
    ...address, kind: 'household', address_id: address.id,
    owner: address.created_by === userId ? 'You' : names.get(address.created_by),
    pinned: address.latitude != null && address.longitude != null,
  }))
  const listApis = !account ? [] : [{ api: account, label: 'mine', title: 'My lists (private)' },
    ...(api ? [{ api, label: 'household', title: `${household.name} (shared)` }] : [])]
  const removable = useCallback((sku) => ((isPrimary ? changingTotals : own)[sku] ?? 0) > 0, [isPrimary, changingTotals, own])
  // Admin: owns the household. Primary user: a member approved as an adult. Everyone else is a child user.
  const isAdult = session?.is_adult ?? session?.user.is_adult
  const roleName = isPrimary ? 'Admin' : isAdult ? 'Primary user' : 'Child user'
  const checkoutBy = !cart?.checkout ? null : cart.checkout.user_id === userId ? 'You' : names.get(cart.checkout.user_id) ?? 'Another member'
  const shop = {
    quantities, cartId, needsHousehold: !household, adultsOnly: Boolean(cart?.adults_only),
    canEdit: cart ? cart.can_edit_items !== false : isPrimary && carts.length === 0, canRemove: removable, onAdjust: adjust,
  }
  const page = showOrderHistory || orderId ? 'orders' : view
  const ready = session && !needsAgeSetup && !restoringSession
  const tabs = [
    ['shop', 'Shop', 'home', () => openPage('shop')],
    ['lists', 'Lists', 'list', () => openPage('lists')],
    ...(household ? [['addresses', 'Addresses', 'pin', () => openPage('addresses')]] : []),
    ...(isPrimary || isAdult ? [['orders', 'Orders', 'orders', () => openPage('orders')]] : []),
    ['household', 'Household', 'people', () => openPage('household')],
  ]

  return <>
    <header className="site-header">
      <div className="header-row shell">
        <button className="brand" onClick={() => openPage('shop')}>familycart</button>
        {ready && household && <button className="location" onClick={() => openPage('addresses')}>
          <Icon name="pin" size={20} /><span><strong>Delivery in minutes</strong>
            <small>{ownAddress ? `${ownAddress.label} · ${ownAddress.city}` : 'Add an address'}</small></span><Icon name="chevron" size={16} />
        </button>}
        {ready ? <label className="search"><Icon name="search" size={18} />
          <input ref={searchInput} type="search" placeholder="Search for milk, eggs, bread" aria-label="Search products" value={query}
            onChange={(event) => {
              if (page !== 'shop') openPage('shop')
              setQuery(event.target.value)
            }} /></label> : <span className="spacer" />}
        {ready && <>
          <button className="icon-button bell" aria-label={`Activity${unread ? `, ${unread} new` : ''}`} onClick={() => openDrawer('activity')}>
            <Icon name="bell" />{unread > 0 && <span className="badge">{unread > 9 ? '9+' : unread}</span>}</button>
          {household && <button className="cart-button" onClick={() => openDrawer('cart')} aria-label={`Open cart, ${itemCount} ${itemCount === 1 ? 'item' : 'items'}`}>
            <Icon name="cart" /><span key={itemCount}>{itemCount > 0 ? <><strong>{itemCount}</strong> <small>{money(cartTotal)}</small></> : 'Cart'}</span></button>}
        </>}
        {session ? <details className="account-menu" onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false
        }}>
          <summary aria-label="Account"><Avatar id={userId} name={session.user.display_name} size="large" /></summary>
          <div onClick={(event) => { if (event.target.closest('button')) event.currentTarget.closest('details').open = false }}>
            <strong>{session.user.display_name}</strong>
            {household && <small className="muted">{household.name} · {roleName}</small>}
            {ready && <>{(isPrimary || isAdult) && <button onClick={() => openPage('orders')}>Order history</button>}
              <button onClick={() => openPage('household')}>{household ? 'Household' : 'Create or join a household'}</button></>}
            <button disabled={busy} onClick={() => run(async () => {
              await flushTaps()
              await logoutSession(token)
              clearSession()
            })}>Sign out</button>
          </div>
        </details> : !restoringSession && <button onClick={() => setAuthOpen(true)}>Login / Register</button>}
      </div>
      {ready && <nav className="site-nav" aria-label="Main">
        {tabs.map(([id, label, icon, open]) => <button key={id} aria-current={id === page ? 'page' : undefined} onClick={open}>
          <Icon name={icon} size={20} /><span>{label}</span></button>)}
      </nav>}
    </header>

    {notices.length > 0 && <div className="cart-activity-stack" role="status" aria-label="Notifications">
      {notices.map((notice) => <div className={`cart-activity-notice ${notice.kind}`} key={notice.id}>
        <Icon name={notice.kind === 'activity' ? 'cart' : notice.kind === 'error' ? 'bell' : 'check'} size={18} /><strong>{notice.text}</strong>
        <button type="button" className="icon-button" aria-label="Dismiss notification"
          onClick={() => setNotices((current) => current.filter((item) => item.id !== notice.id))}><Icon name="close" size={16} /></button>
      </div>)}
    </div>}

    <main className="shell">
      {ready && pendingOrder && drawer !== 'checkout' && <div className="banner row" role="status">
        <span><strong>Your last order was not confirmed</strong><p>Check whether it went through before ordering again.</p></span>
        <button onClick={() => { setCheckoutCart(pendingOrder.cart); setDrawer('checkout') }}>Resume checkout</button>
      </div>}
      {restoringSession ? <div className="page" role="status" aria-label="Restoring your session">
        <div className="shop-intro skeleton" /><div className="product-grid">
          {Array.from({ length: 12 }, (_, index) => <div className="product-card skeleton" key={index} />)}</div></div> :
        !session ? <Landing onOpenAuth={() => setAuthOpen(true)} /> :
        needsAgeSetup ? <section className="panel onboarding page">
          <h1>Finish account setup</h1>
          <p className="muted">Add your date of birth to complete your account.</p>
          <DateOfBirthForm busy={busy} onSubmit={(birth) => run(async () => {
            try { await account.saveAge(birth) }
            catch (issue) { if (issue.status !== 409) throw issue }
            await hydrateSession(await restoreSession())
          })} />
        </section> :
        showOrderHistory ? <OrderHistory key={userId} account={account} onOpen={openOrder} onShop={() => openPage('shop')} /> :
        orderId ? <OrderConfirmation error={orderError} onBack={() => openPage('orders')} onShop={() => openPage('shop')} products={bySku}
          order={confirmation?.order_id === orderId && confirmation.purchaser_id === userId ? confirmation : null} /> :
        needsOwnAddress || (view === 'addresses' && household) ?
          <AddressesPage key={household.id} required={Boolean(needsOwnAddress)} household={household} userId={userId} members={members} {...addressBook} /> :
        view === 'lists' ? <ListsPage account={account} api={api} token={token} household={household} user={session.user} products={products}
          carts={carts} defaultCartId={cartId} onBack={() => openPage('shop')} onNotify={showPopup}
          onCartChanged={(id) => { if (id === cartId) reloadCart(id) }}
          onAccessLost={(reason) => {
            setView('shop')
            showPopup(reason || 'Your household access was revoked. Contact the household admin.', 'error')
          }} /> :
        view === 'household' ? <HouseholdPage household={household} households={households} session={session} carts={carts} members={members}
          addresses={addresses} account={account} busy={busy} invitation={invitation} onNotify={showPopup}
          roleName={roleName} memberRequests={memberRequests}
          onLeave={() => endMembership(() => api.leave(), `You left ${household.name}.`)}
          onDelete={() => endMembership(() => api.deleteHousehold(), `${household.name} was deleted.`)}
          onRequestMember={(name, note) => run(async () => {
            const created = await api.requestMember(name, note)
            setMemberRequests((current) => [...current.filter((item) => item.id !== created.id), created])
            showPopup('Request sent to the admin.')
            return created
          })}
          onDecideRequest={(id, approved) => run(async () => {
            await api.decideMemberRequest(id, approved)
            setMemberRequests(await api.memberRequests())
            showPopup(approved ? 'Request approved. The person who asked can now collect the invitation.' : 'Request declined.')
          })}
          onCollectInvitation={(id) => run(async () => {
            setInvitation(await api.requestedInvitation(id))
            setMemberRequests(await api.memberRequests())
          })}
          onCreate={(name, address) => run(async () => {
            const created = await account.createHousehold(name, session.user.display_name, address)
            setHouseholds(await account.households())
            await loadHousehold(created)
            showPopup('Household created.')
            return created
          })}
          onJoin={(code, address) => run(async () => {
            const joined = await account.acceptInvitation(code, session.user.display_name, address)
            setHouseholds(await account.households())
            await loadHousehold(joined)
            showPopup('You joined the household.')
            return joined
          })}
          onSelectHousehold={(id) => run(async () => {
            await flushTaps()
            await loadHousehold(households.find((item) => item.id === id))
          })}
          onInvite={() => run(async () => setInvitation(await api.createInvitation()))}
          onRevoke={(id) => run(async () => { await api.revokeMember(id); await refreshMembers() })}
          onDecideAge={(id, approved) => run(async () => {
            await api.decideAge(id, approved)
            await refreshMembers()
            showPopup(approved ? 'They are now a primary user.' : 'They are now a child user.')
          })}
          onTransferHousehold={(id) => run(async () => {
            await api.transferHouseholdOwnership(id)
            const updated = await account.households()
            setHouseholds(updated)
            await loadHousehold(updated.find((item) => item.id === householdId))
            showPopup('Household ownership transferred.')
          })}
          onOpenAddresses={() => openPage('addresses')} onOpenLists={() => openPage('lists')} onOpenCart={() => openDrawer('cart')} /> :
        <ShopPage products={products} loading={products.length === 0} query={query} onClearQuery={() => setQuery('')} shop={shop} carts={carts}
          household={household} roleName={roleName} checkoutBy={checkoutBy} userName={session.user.display_name} listApis={listApis} onSelectCart={selectCart}
          peopleFor={(sku) => (cart ? combined(cart) : []).filter((row) => row.sku === sku).map((row) => ({
            name: row.contributor_id === userId ? 'You' : names.get(row.contributor_id) ?? 'Member', quantity: row.quantity,
          }))}
          onSetUpHousehold={() => openPage('household')} onOpenCart={() => openDrawer('cart')} onNotify={showPopup} />}
    </main>

    {ready && ((['cart', 'checkout'].includes(drawer) && (household || checkoutCart)) || drawer === 'activity') &&
      <Overlay kind="drawer" label={drawer === 'activity' ? 'Activity' : drawer === 'checkout' ? 'Checkout' : 'Household carts'}
        onClose={() => { setDrawer(null); setCheckoutCart(null) }}>{(leave) => <>
        <div className="drawer-head">
          {drawer === 'checkout' ? <button className="text-button" onClick={() => { setCheckoutCart(null); setDrawer(household ? 'cart' : null) }}>← Back to cart</button>
            : <h2>{drawer === 'activity' ? 'Activity' : 'Household carts'}</h2>}
          <button className="icon-button" aria-label="Close" onClick={leave}><Icon name="close" /></button>
        </div>
        {drawer === 'activity' ? <ActivityList activity={activity} onClear={() => setActivity([])} /> :
          drawer === 'checkout' && checkoutCart ? <Checkout key={checkoutCart.cart_id} account={account} cart={checkoutCart} pending={pendingOrder}
            onPlace={submitOrder} onOpenOrder={resolveOrder} isAdult={session.is_adult ?? session.user.is_adult}
            allowed={householdId === checkoutCart.household_id && cart?.id === checkoutCart.cart_id && Boolean(cart.can_checkout)}
            changes={checkoutChanges} waiting={waitingRows}
            rows={available.filter((row) => selected.has(row.sku))} user={session.user}
            names={names} products={bySku} addresses={checkoutAddresses}
            editor={api && <AddressBook {...addressBook} buttonLabel="Save address" />} /> :
          <CartView carts={carts} cart={cart} cartId={cartId} rows={rows} user={session.user} household={household} members={members} names={names}
            grants={grants} selected={selected} busy={busy} live={live} saving={Object.keys(taps).length + Object.keys(sending).length > 0}
            canAdultsOnly={session.can_create_adults_only_cart} waitingRows={waitingRows} checkoutBy={checkoutBy}
            mine={cart?.checkout?.user_id === userId} canRemove={removable} onEndCheckout={() => run(async () => {
              await api.leaveCheckout(cartId)
              await reloadCart(cartId)
            })} onSelectCart={selectCart} onAdjust={adjust} onRemove={removeItem} onCheckout={startCheckout}
            onToggle={(sku) => {
              const next = new Set(selected)
              if (!next.delete(sku)) next.add(sku)
              setAllSelected(false)
              setSelected(next)
            }}
            onSelectAll={(all) => { setAllSelected(all); setSelected(new Set()) }}
            onChangeGrant={(member, grant) => run(async () => {
              await api.changeGrant(cartId, member, grant)
              setGrants(await api.grants(cartId))
            })}
            onCreateCart={(name, adultsOnly) => run(async () => {
              const created = await api.create(name, adultsOnly)
              setCarts(await api.carts())
              selectCart(created.id)
              showPopup(`${created.name} created.`)
            })}
            onDeleteCart={() => run(async () => {
              const { name } = cart
              await flushTaps()
              await api.deleteCart(cartId)
              const remaining = await api.carts()
              forgetCart()
              setCartId(null)
              cartIdRef.current = null
              setCarts(remaining)
              showPopup(`${name} deleted.`)
            })} />}
      </>}</Overlay>}

    {authOpen && !restoringSession && !session && <GoogleAuthModal busy={busy} onClose={() => setAuthOpen(false)}
      onAuthenticated={(signedIn) => run(async () => {
        await hydrateSession(signedIn)
        setAuthOpen(false)
        showPopup('Welcome back.')
      })} />}
    <ConfirmHost />
  </>
}

createRoot(document.getElementById('root')).render(<App />)
