import axios from 'axios'

const client = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 30_000,
})

client.interceptors.response.use(
  (res) => res,
  (err) => {
    const message = err.response?.data?.detail || err.message || 'Unknown error'
    console.error(`[fade-out API] ${err.config?.method?.toUpperCase()} ${err.config?.url}: ${message}`)
    return Promise.reject(err)
  },
)

export default client
