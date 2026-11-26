// Cart changes this device has made and the server has not confirmed yet.
// The server's database stays the source of truth for what is in a cart.

const DATABASE = 'familycart'
const OPERATIONS = 'operations'

function finished(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

function committed(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = transaction.onabort = () => reject(transaction.error)
  })
}

async function openIndexedDb() {
  const request = window.indexedDB.open(DATABASE, 1)
  request.onupgradeneeded = () => {
    request.result.createObjectStore(OPERATIONS, { keyPath: 'requestId' }).createIndex('userId', 'userId')
  }
  const db = await finished(request)
  db.onversionchange = () => db.close()
  const change = (action) => {
    const transaction = db.transaction(OPERATIONS, 'readwrite')
    action(transaction.objectStore(OPERATIONS))
    return committed(transaction)
  }
  return {
    load: (userId) => finished(db.transaction(OPERATIONS).objectStore(OPERATIONS).index('userId').getAll(userId)),
    put: (operation) => change((store) => store.put(operation)),
    remove: (id) => change((store) => store.delete(id)),
  }
}

/** Keeps changes only while the page is open: for tests, and for browsers that block IndexedDB. */
export function memoryStore(saved = new Map()) {
  return {
    load: async (userId) => [...saved.values()].filter((operation) => operation.userId === userId),
    put: async (operation) => { saved.set(operation.requestId, structuredClone(operation)) },
    remove: async (id) => { saved.delete(id) },
  }
}

export async function openStore() {
  try { return await openIndexedDb() } catch { return memoryStore() }
}
