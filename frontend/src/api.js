const api = '/api/v1'
let activeAccessToken = null
let accessTokenListener = () => {}
let refreshPromise = null

export class ApiError extends Error {
  constructor(status, message, detail = null) {
    super(message)
    this.status = status
    this.code = detail?.code
    this.detail = detail
  }
}

function publishAccessToken(token, user) {
  activeAccessToken = token
  accessTokenListener(token, user)
}

export function onAccessTokenChanged(listener) {
  accessTokenListener = listener
  return () => { accessTokenListener = () => {} }
}

export function clearAccessToken() {
  publishAccessToken(null)
}

async function requestRefreshedSession() {
  const response = await fetch(`${api}/sessions/refresh`, {
    method: 'POST', credentials: 'include', cache: 'no-store',
  })
  const data = await response.json().catch(() => null)
  if (!response.ok || !data?.access_token) {
    // Only a definite "no" ends the session. A server problem is not a verdict.
    if (response.status === 401) publishAccessToken(null)
    throw new ApiError(response.status, typeof data?.detail === 'string' ? data.detail : 'Your session ended.')
  }
  publishAccessToken(data.access_token, data.user)
  return data
}

export function restoreSession() {
  if (refreshPromise) return refreshPromise
  refreshPromise = requestRefreshedSession().finally(() => { refreshPromise = null })
  return refreshPromise
}

export async function refreshAccessToken() {
  return (await restoreSession()).access_token
}

async function request(path, { token, method = 'GET', body, retry = true, signal, idempotencyKey, keepalive } = {}) {
  const headers = {}
  const bearer = token ? (activeAccessToken || token) : null
  if (bearer) headers.Authorization = `Bearer ${bearer}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  const response = await fetch(path, {
    method, headers, body: JSON.stringify(body), cache: 'no-store', credentials: 'include', signal, keepalive,
  })
  if (response.status === 401 && token && retry) {
    signal?.throwIfAborted()
    const refreshedToken = await refreshAccessToken()
    return request(path, { token: refreshedToken, method, body, retry: false, signal, idempotencyKey, keepalive })
  }
  if (response.status === 204) return

  const data = await response.json().catch(() => null)
  if (!response.ok || data === null) {
    const detail = data?.detail
    throw new ApiError(response.status, errorMessage(detail, response.status), detail)
  }
  return data
}

/** A readable message from an error response: plain text, {message}, or FastAPI's 422 validation list. */
function errorMessage(detail, status) {
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  if (Array.isArray(detail) && detail.length > 0) {
    const [first] = detail
    const field = Array.isArray(first.loc) ? first.loc.filter((part) => part !== 'body').join('.') : ''
    return field ? `${field}: ${first.msg}` : String(first.msg ?? `Request failed (${status}).`)
  }
  if (status === 422) return 'The server did not accept this request. Try reloading the page.'
  return `Request failed (${status}).`
}

export async function signInWithGoogle(credential, dateOfBirth) {
  const session = await request(`${api}/auth/google`, {
    method: 'POST', body: { credential, date_of_birth: dateOfBirth },
  })
  publishAccessToken(session.access_token)
  return session
}

export function googleConfiguration() {
  return request(`${api}/auth/google/config`)
}

export async function logoutSession(token) {
  try {
    await request(`${api}/sessions/logout`, { token, method: 'POST', retry: false })
  } catch (error) {
    // 401: the session has already ended. Anything else: it may still be active.
    if (error.status !== 401) {
      throw new ApiError(error.status ?? 0, 'Sign-out could not be completed, so you are still signed in. Try again.')
    }
  }
  publishAccessToken(null)
}

function addressApi(token, path) {
  const addressPath = (id) => `${path}/${encodeURIComponent(id)}`
  return {
    addresses: () => request(path, { token }),
    createAddress: (address) => request(path, { token, method: 'POST', body: address }),
    deleteAddress: (id) => request(addressPath(id), { token, method: 'DELETE' }),
    updateAddressPin: (id, pin) => request(addressPath(id), { token, method: 'PATCH', body: pin }),
    resolveAddress: (id, signal) => request(`${addressPath(id)}/resolve-store`, { token, method: 'POST', signal }),
    ...mapsApi(token),
  }
}

function mapsApi(token) {
  return {
    mapStores: (signal) => request(`${api}/maps/stores`, { token, signal }),
    previewPin: (pin, signal) => request(`${api}/maps/resolve`, { token, method: 'POST', body: pin, signal }),
  }
}

/** Shopping lists. My lists and household lists share one shape under different paths. */
function listApi(token, lists) {
  const listPath = (id) => `${lists}/${encodeURIComponent(id)}`
  return {
    lists: () => request(lists, { token }),
    createList: (name) => request(lists, { token, method: 'POST', body: { name } }),
    list: (id) => request(listPath(id), { token }),
    renameList: (id, name) => request(listPath(id), { token, method: 'PATCH', body: { name } }),
    deleteList: (id) => request(listPath(id), { token, method: 'DELETE' }),
    addListItem: (id, item) => request(`${listPath(id)}/items`, { token, method: 'POST', body: item }),
    addListItems: (id, items) => request(`${listPath(id)}/items/bulk`, { token, method: 'POST', body: { items } }),
    changeList: (id, changes) => request(`${listPath(id)}/changes`, { token, method: 'POST', body: changes }),
    updateListItem: (id, itemId, changes) => request(`${listPath(id)}/items/${encodeURIComponent(itemId)}`, {
      token, method: 'PATCH', body: changes,
    }),
    deleteListItem: (id, itemId) => request(`${listPath(id)}/items/${encodeURIComponent(itemId)}`, {
      token, method: 'DELETE',
    }),
    clearCheckedItems: (id) => request(`${listPath(id)}/items/clear-checked`, { token, method: 'POST' }),
    moveListToCart: (id, target, itemIds, householdId) => request(`${listPath(id)}/move-to-cart`, {
      token, method: 'POST', body: { target, item_ids: itemIds, ...(householdId ? { household_id: householdId } : {}) },
    }),
  }
}

export function accountApi(token) {
  if (token) activeAccessToken = token
  return {
    ...mapsApi(token),
    households: () => request(`${api}/households`, { token }),
    createHousehold: (name, displayName, address) => request(`${api}/households`, {
      token, method: 'POST', body: { name, display_name: displayName, address },
    }),
    acceptInvitation: (invitationCode, displayName, address) => request(`${api}/household-invitations/accept`, {
      token, method: 'POST', body: { invitation_code: invitationCode, display_name: displayName, address },
    }),
    ...listApi(token, `${api}/my-lists`),
    products: () => request(`${api}/products`, { token }),
    saveAge: (dateOfBirth) => request(`${api}/auth/age`, {
      token, method: 'POST', body: { date_of_birth: dateOfBirth },
    }),
    quote: (body) => request(`${api}/checkout/quotes`, { token, method: 'POST', body }),
    placeOrder: (body, idempotencyKey) => request(`${api}/checkout/orders`, {
      token, method: 'POST', body, idempotencyKey,
    }),
    order: (id, signal) => request(`${api}/orders/${encodeURIComponent(id)}`, { token, signal }),
    orders: (before, signal) => request(
      before ? `${api}/orders?${new URLSearchParams({ before })}` : `${api}/orders`, { token, signal },
    ),
  }
}

export function householdApi(token, householdId) {
  if (token) activeAccessToken = token
  const household = `${api}/households/${encodeURIComponent(householdId)}`
  const carts = `${household}/carts`
  const cartPath = (id) => `${carts}/${encodeURIComponent(id)}`
  const contributionPath = (id, sku) => `${cartPath(id)}/items/${encodeURIComponent(sku)}/contributions`
  return {
    ...addressApi(token, `${household}/addresses`),
    carts: () => request(carts, { token }),
    create: (name, adultsOnly) => request(carts, { token, method: 'POST', body: { name, adults_only: adultsOnly } }),
    capabilities: () => request(`${household}/capabilities`, { token }),
    cart: (id) => request(cartPath(id), { token }),
    // Opens checkout, or keeps it open, and returns the cart as it is now.
    startCheckout: (id) => request(`${cartPath(id)}/checkout`, { token, method: 'POST' }),
    // keepalive lets this finish while the page is closing.
    leaveCheckout: (id) => request(`${cartPath(id)}/checkout`, { token, method: 'DELETE', keepalive: true }),
    leave: () => request(`${household}/leave`, { token, method: 'POST' }),
    deleteHousehold: () => request(household, { token, method: 'DELETE' }),
    memberRequests: () => request(`${household}/member-requests`, { token }),
    requestMember: (name, note) => request(`${household}/member-requests`, { token, method: 'POST', body: { name, note } }),
    decideMemberRequest: (id, approved) => request(`${household}/member-requests/${encodeURIComponent(id)}`, {
      token, method: 'PUT', body: { approved },
    }),
    requestedInvitation: (id) => request(`${household}/member-requests/${encodeURIComponent(id)}/invitation`, { token, method: 'POST' }),
    deleteCart: (id) => request(cartPath(id), { token, method: 'DELETE' }),
    members: () => request(`${household}/members?include_roles=true`, { token }),
    transferHouseholdOwnership: (memberId) => request(
      `${household}/members/${encodeURIComponent(memberId)}/primary`, { token, method: 'PUT' },
    ),
    revokeMember: (id) => request(`${household}/members/${encodeURIComponent(id)}`, { token, method: 'DELETE' }),
    createInvitation: () => request(`${household}/invitations`, { token, method: 'POST' }),
    grants: (id) => request(`${cartPath(id)}/grants?include_creator=true`, { token }),
    changeGrant: (id, member, grant) => request(`${cartPath(id)}/grants/${encodeURIComponent(member)}`, {
      token, method: grant ? 'PUT' : 'DELETE',
    }),
    // What this household allows, which depends on whether the primary approved the caller's age.
    products: () => request(`${api}/products?${new URLSearchParams({ household_id: householdId })}`, { token }),
    decideAge: (memberId, approved) => request(`${household}/members/${encodeURIComponent(memberId)}/age`, {
      token, method: 'PUT', body: { approved },
    }),
    ...listApi(token, `${household}/lists`),
    setQuantity: (id, sku, quantity) => request(`${contributionPath(id, sku)}/me`, {
      token, method: 'PUT', body: { quantity },
    }),
    adjustQuantity: (id, sku, delta) => request(`${contributionPath(id, sku)}/me`, {
      token, method: 'PATCH', body: { delta },
    }),
    adjustSharedQuantity: (id, sku, delta) => request(`${cartPath(id)}/items/${encodeURIComponent(sku)}`, {
      token, method: 'PATCH', body: { delta },
    }),
    removeSharedItem: (id, sku) => request(`${cartPath(id)}/items/${encodeURIComponent(sku)}`, {
      token, method: 'DELETE',
    }),
    removeContribution: (id, sku, contributor) => request(`${contributionPath(id, sku)}/${encodeURIComponent(contributor)}`, {
      token, method: 'DELETE',
    }),
  }
}

export function connectLists(token, householdId) {
  const url = new URL(`${api}/households/${encodeURIComponent(householdId)}/lists/live`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(url)
  socket.addEventListener('open', () => socket.send(JSON.stringify({ token: activeAccessToken || token })))
  return socket
}

export function connectCarts(token, householdId, cartId) {
  const household = `${api}/households/${encodeURIComponent(householdId)}`
  const url = new URL(`${household}/live`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (cartId) url.searchParams.set('cart_id', cartId)
  const socket = new WebSocket(url)
  socket.addEventListener('open', () => socket.send(JSON.stringify({ token: activeAccessToken || token })))
  return socket
}
