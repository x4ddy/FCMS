const attemptStorageKey = (userId) => `familycart:checkout:${userId}`

export function readAttempt(userId, storage = window.sessionStorage) {
  const saved = storage.getItem(attemptStorageKey(userId))
  if (!saved) return null
  try { return JSON.parse(saved) } catch { return null }
}

export function saveAttempt(userId, attempt, storage = window.sessionStorage) {
  if (attempt) storage.setItem(attemptStorageKey(userId), JSON.stringify(attempt))
  else storage.removeItem(attemptStorageKey(userId))
}

export function isUnconfirmed(error) {
  return !error.status || error.status >= 500 || [401, 408, 429].includes(error.status)
    || error.code === 'IDEMPOTENCY_KEY_REUSED'
}
