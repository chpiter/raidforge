<template>
  <el-drawer
    :model-value="modelValue"
    :title="title"
    size="580px"
    :close-on-click-modal="false"
    @update:model-value="onVisible"
  >
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <div v-if="!tasks.length && !error" class="rf-muted">等待任务状态...</div>

    <div v-for="task in tasks" :key="task.task_id" class="task-card">
      <div class="task-head">
        <StatusTag :status="task.status" />
        <span class="rf-mono">{{ task.type }}</span>
        <span class="rf-muted">{{ task.phase }}</span>
        <div class="spacer" />
        <el-button link type="primary" @click="openTask(task.task_id)">打开任务详情</el-button>
      </div>

      <el-progress
        v-if="percent(task) !== null"
        :percentage="percent(task)"
        :status="task.status === 'failed' ? 'exception' : undefined"
      />
      <p class="task-msg">
        {{ task.progress?.message || task.error_message || '执行中...' }}
        <span v-if="task.progress?.done !== null && task.progress?.done !== undefined">
          （{{ task.progress.done }} / {{ task.progress.total ?? '?' }}）
        </span>
      </p>

      <el-alert
        v-if="task.error_code"
        type="error"
        :closable="false"
        show-icon
        :title="`${task.error_code}: ${task.error_message || ''}`"
      />

      <EventTimeline :items="(task.events || []).slice(-5)" />
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import StatusTag from './StatusTag.vue'
import EventTimeline from './EventTimeline.vue'
import { getTask } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  taskIds: { type: Array, default: () => [] },
  title: { type: String, default: '任务进度' },
  pollInterval: { type: Number, default: 2000 }
})

const emit = defineEmits(['update:modelValue', 'finished', 'closed'])
const router = useRouter()
const tasks = ref([])
const error = ref(null)
const TERMINAL = new Set(['finished', 'failed', 'cancelled', 'expired'])
let timer = null
let finishedEmitted = false

const anyFailed = computed(() =>
  tasks.value.some((task) => ['failed', 'cancelled', 'expired'].includes(task.status))
)

const isTerminal = (status) => TERMINAL.has(status)

const percent = (task) => {
  const done = Number(task.progress?.done)
  const total = Number(task.progress?.total)
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
}

const stop = () => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

const load = async () => {
  if (!props.modelValue || !props.taskIds.length) return
  try {
    const results = await Promise.all(props.taskIds.map((id) => getTask(id)))
    tasks.value = results.map((item) => item.task)
    error.value = null
    if (
      tasks.value.length &&
      tasks.value.every((task) => isTerminal(task.status)) &&
      !finishedEmitted
    ) {
      finishedEmitted = true
      stop()
      emit('finished', { taskIds: [...props.taskIds], anyFailed: anyFailed.value })
    }
  } catch (exc) {
    error.value = exc
  }
}

const start = () => {
  stop()
  finishedEmitted = false
  tasks.value = []
  error.value = null
  load()
  timer = setInterval(load, Math.max(500, props.pollInterval))
}

watch(
  () => [props.modelValue, props.taskIds],
  ([visible]) => {
    if (visible) start()
    else stop()
  },
  { deep: true }
)

onUnmounted(stop)

const onVisible = (value) => {
  emit('update:modelValue', value)
  if (!value) {
    stop()
    emit('closed')
  }
}

const close = () => onVisible(false)

const openTask = (taskId) => {
  close()
  router.push(`/tasks/${taskId}`)
}
</script>

<style scoped>
.task-card {
  border: 1px solid var(--rf-border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
  background: #fff;
}

.task-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.task-msg {
  font-size: 13px;
  margin: 8px 0;
}
</style>
