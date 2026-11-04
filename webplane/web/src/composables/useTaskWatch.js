import { onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getTask } from '../api'

const TERMINAL = new Set(['finished', 'failed', 'cancelled', 'expired'])
const FAILED = new Set(['failed', 'cancelled', 'expired'])

/**
 * Headless task watcher: the task runs server-side, the page only polls for
 * completion and then refreshes its data. No drawer/modal is involved.
 */
export function useTaskWatch() {
  const timers = new Set()

  const stopAll = () => {
    for (const timer of timers) clearInterval(timer)
    timers.clear()
  }

  onUnmounted(stopAll)

  function watchTasks(taskIds, options = {}) {
    const ids = (taskIds || []).filter(Boolean)
    if (!ids.length) return

    const {
      title = '任务',
      silent = false,
      pollInterval = 2000,
      timeoutMs = 15 * 60 * 1000,
      failureHint = '',
      onFinished = null
    } = options

    let stopped = false
    let finished = false
    let errors = 0
    const startedAt = Date.now()
    let timer = null

    const stop = () => {
      if (timer) {
        clearInterval(timer)
        timers.delete(timer)
        timer = null
      }
      stopped = true
    }

    const finish = (tasks) => {
      if (finished) return
      finished = true
      stop()
      const failed = tasks.filter((task) => FAILED.has(task.status))
      const anyFailed = failed.length > 0
      const first = failed[0] || null
      if (!silent) {
        if (anyFailed) {
          const code = first?.error_code ? `: ${first.error_code}` : ''
          const hint = failureHint ? `；${failureHint}` : ''
          ElMessage.warning(`${title}失败${code}${hint}`)
        } else {
          ElMessage.success(`${title}完成`)
        }
      }
      if (onFinished) {
        onFinished({
          taskIds: [...ids],
          anyFailed,
          tasks,
          error_code: first?.error_code || '',
          error_message: first?.error_message || ''
        })
      }
    }

    const tick = async () => {
      if (stopped) return
      if (Date.now() - startedAt > timeoutMs) {
        stop()
        if (!silent) ElMessage.warning(`${title}超时，请到任务页查看`)
        return
      }
      try {
        const results = await Promise.all(ids.map((id) => getTask(id)))
        errors = 0
        const tasks = results.map((item) => item.task).filter(Boolean)
        if (tasks.length && tasks.every((task) => TERMINAL.has(task.status))) {
          finish(tasks)
        }
      } catch (exc) {
        errors += 1
        if (errors >= 3) {
          stop()
          if (!silent) ElMessage.warning(`${title}状态获取失败，请到任务页查看`)
        }
      }
    }

    tick()
    timer = setInterval(tick, Math.max(500, pollInterval))
    timers.add(timer)
  }

  return { watchTasks, stopAll }
}
