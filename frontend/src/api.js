const household = '/households/demo-household'
const carts = `${household}/carts`
const cartPath = (id) => `${carts}/${encodeURIComponent(id)}`
const contributionPath = (id, sku) => `${cartPath(id)}/items/${encodeURIComponent(sku)}/contributions`

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function request(path, { token, method = 'GET', body } = {}) {
  const headers = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, { method, headers, body: JSON.stringify(body), cache: 'no-store' })
  if (response.status === 204) return

  const data = await response.json().catch(() => null)
  if (!response.ok || data === null) {
    const message = typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}).`
    throw new ApiError(response.status, message)
  }
  return data
}

export function signIn(identifier, password) {
  return request('/auth/login', { method: 'POST', body: { identifier, password } })
}

export function householdApi(token) {
  return {
    carts: () => request(carts, { token }),
    create: (name, adultsOnly) => request(carts, { token, method: 'POST', body: { name, adults_only: adultsOnly } }),
    capabilities: () => request(`${household}/capabilities`, { token }),
    cart: (id) => request(cartPath(id), { token }),
    deleteCart: (id) => request(cartPath(id), { token, method: 'DELETE' }),
    members: () => request(`${household}/members?include_roles=true`, { token }),
    promoteMember: (id) => request(`${household}/members/${encodeURIComponent(id)}/primary`, { token, method: 'PUT' }),
    grants: (id) => request(`${cartPath(id)}/grants?include_creator=true`, { token }),
    changeGrant: (id, member, grant) => request(`${cartPath(id)}/grants/${encodeURIComponent(member)}`, {
      token, method: grant ? 'PUT' : 'DELETE',
    }),
    products: () => request('/products', { token }),
    setQuantity: (id, sku, quantity) => request(`${contributionPath(id, sku)}/me`, {
      token, method: 'PUT', body: { quantity },
    }),
    adjustQuantity: (id, sku, delta) => request(`${contributionPath(id, sku)}/me`, {
      token, method: 'PATCH', body: { delta },
    }),
    removeContribution: (id, sku, contributor) => request(`${contributionPath(id, sku)}/${encodeURIComponent(contributor)}`, {
      token, method: 'DELETE',
    }),
  }
}

export function connectCarts(token, cartId) {
  const url = new URL(`${household}/live`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (cartId) url.searchParams.set('cart_id', cartId)
  const socket = new WebSocket(url)
  socket.addEventListener('open', () => socket.send(JSON.stringify({ token })))
  return socket
}
