import { onMounted, onUnmounted, ref } from 'vue'

export function usePolling(fn, interval = 5000, immediate = true) {
  const loading = ref(false)
  const error = ref(null)
  const lastUpdated = ref(null)
  let timer = null

  const run = async () => {
    if (document.hidden) return
    loading.value = true
    try {
      await fn()
      error.value = null
      lastUpdated.value = new Date()
    } catch (exc) {
      error.value = exc
    } finally {
      loading.value = false
    }
  }

  const stop = () => {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  const start = () => {
    stop()
    if (immediate) run()
    timer = setInterval(run, interval)
  }

  onMounted(start)
  onUnmounted(stop)

  return { run, stop, start, loading, error, lastUpdated }
}

export function relativeTime(value) {
  if (!value) return '-'
  const moment = new Date(value)
  if (Number.isNaN(moment.getTime())) return value
  const seconds = Math.max(0, Math.round((Date.now() - moment.getTime()) / 1000))
  if (seconds < 60) return `${seconds}s 前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h 前`
  return `${Math.floor(seconds / 86400)}d 前`
}

export function formatTime(value) {
  if (!value) return '-'
  const moment = new Date(value)
  if (Number.isNaN(moment.getTime())) return value
  return moment.toLocaleString()
}

export function formatBytes(value) {
  const size = Number(value) || 0
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`
}
