const api = '/api/v1'
let activeAccessToken = null
let accessTokenListener = () => {}
let refreshPromise = null

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

function publishAccessToken(token) {
  activeAccessToken = token
  accessTokenListener(token)
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
    publishAccessToken(null)
    throw new ApiError(response.status, typeof data?.detail === 'string' ? data.detail : 'Your session ended.')
  }
  publishAccessToken(data.access_token)
  return data
}

export function restoreSession() {
  return requestRefreshedSession()
}

export function refreshAccessToken() {
  if (refreshPromise) return refreshPromise
  refreshPromise = (async () => {
    const session = await requestRefreshedSession()
    return session.access_token
  })().finally(() => { refreshPromise = null })
  return refreshPromise
}

async function request(path, { token, method = 'GET', body, retry = true } = {}) {
  const headers = {}
  const bearer = token ? (activeAccessToken || token) : null
  if (bearer) headers.Authorization = `Bearer ${bearer}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, {
    method, headers, body: JSON.stringify(body), cache: 'no-store', credentials: 'include',
  })
  if (response.status === 401 && token && retry) {
    const refreshedToken = await refreshAccessToken()
    return request(path, { token: refreshedToken, method, body, retry: false })
  }
  if (response.status === 204) return

  const data = await response.json().catch(() => null)
  if (!response.ok || data === null) {
    const message = typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}).`
    throw new ApiError(response.status, message)
  }
  return data
}

export async function signInWithGoogle(credential) {
  const session = await request(`${api}/auth/google`, {
    method: 'POST', body: { credential },
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
  } finally {
    publishAccessToken(null)
  }
}

export function accountApi(token) {
  if (token) activeAccessToken = token
  return {
    households: () => request(`${api}/households`, { token }),
    createHousehold: (name, displayName) => request(`${api}/households`, {
      token, method: 'POST', body: { name, display_name: displayName },
    }),
    acceptInvitation: (invitationCode, displayName) => request(`${api}/household-invitations/accept`, {
      token, method: 'POST', body: { invitation_code: invitationCode, display_name: displayName },
    }),
    personalCart: () => request(`${api}/personal-cart`, { token }),
    addresses: () => request(`${api}/addresses`, { token }),
    createAddress: (address) => request(`${api}/addresses`, { token, method: 'POST', body: address }),
    deleteAddress: (id) => request(`${api}/addresses/${encodeURIComponent(id)}`, {
      token, method: 'DELETE',
    }),
    adjustPersonalItem: (sku, delta) => request(`${api}/personal-cart/items/${encodeURIComponent(sku)}`, {
      token, method: 'PATCH', body: { delta },
    }),
    products: () => request(`${api}/products`, { token }),
  }
}

export function householdApi(token, householdId) {
  if (token) activeAccessToken = token
  const household = `${api}/households/${encodeURIComponent(householdId)}`
  const carts = `${household}/carts`
  const cartPath = (id) => `${carts}/${encodeURIComponent(id)}`
  const contributionPath = (id, sku) => `${cartPath(id)}/items/${encodeURIComponent(sku)}/contributions`
  return {
    carts: () => request(carts, { token }),
    create: (name, adultsOnly) => request(carts, { token, method: 'POST', body: { name, adults_only: adultsOnly } }),
    capabilities: () => request(`${household}/capabilities`, { token }),
    cart: (id) => request(cartPath(id), { token }),
    deleteCart: (id) => request(cartPath(id), { token, method: 'DELETE' }),
    members: () => request(`${household}/members?include_roles=true`, { token }),
    transferHouseholdOwnership: (memberId) => request(
      `${household}/members/${encodeURIComponent(memberId)}/primary`, { token, method: 'PUT' },
    ),
    revokeMember: (id) => request(`${household}/members/${encodeURIComponent(id)}`, { token, method: 'DELETE' }),
    createInvitation: () => request(`${household}/invitations`, { token, method: 'POST' }),
    addresses: () => request(`${household}/addresses`, { token }),
    createAddress: (address) => request(`${household}/addresses`, { token, method: 'POST', body: address }),
    deleteAddress: (id) => request(`${household}/addresses/${encodeURIComponent(id)}`, { token, method: 'DELETE' }),
    grants: (id) => request(`${cartPath(id)}/grants?include_creator=true`, { token }),
    changeGrant: (id, member, grant) => request(`${cartPath(id)}/grants/${encodeURIComponent(member)}`, {
      token, method: grant ? 'PUT' : 'DELETE',
    }),
    products: () => request(`${api}/products`, { token }),
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

export function connectCarts(token, householdId, cartId) {
  const household = `${api}/households/${encodeURIComponent(householdId)}`
  const url = new URL(`${household}/live`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (cartId) url.searchParams.set('cart_id', cartId)
  const socket = new WebSocket(url)
  socket.addEventListener('open', () => socket.send(JSON.stringify({ token: activeAccessToken || token })))
  return socket
}
