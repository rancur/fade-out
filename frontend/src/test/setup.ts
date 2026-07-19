// Vitest setup: minimal DOM API stubs missing from jsdom.

class IntersectionObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return []
  }
}

if (typeof globalThis.IntersectionObserver === 'undefined') {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  globalThis.IntersectionObserver = IntersectionObserverStub as any
}
