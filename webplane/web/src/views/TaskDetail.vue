<template>
  <div class="rf-page">
    <PageHeader :title="task?.task_id || '任务详情'" :subtitle="task ? `${task.type} · ${task.agent_id}` : ''">
      <template #actions>
        <el-button @click="$router.push('/tasks')">返回列表</el-button>
        <el-button :loading="loading" @click="run">刷新</el-button>
        <el-button @click="copyJson">复制 JSON</el-button>
        <el-button v-if="task && !isTerminal(task.status)" type="danger" @click="cancel">取消任务</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <template v-if="task">
      <div class="rf-panel">
        <div class="rf-toolbar" style="margin-bottom: 12px">
          <StatusTag :status="task.status" />
          <el-tag effect="plain" size="small">phase: {{ task.phase }}</el-tag>
          <el-tag v-if="task.cancel_requested" type="warning" effect="plain" size="small">取消请求已发送</el-tag>
          <div class="spacer" />
          <span class="rf-muted">证据状态: {{ task.evidence_status }}</span>
        </div>
        <el-descriptions :column="3" border>
          <el-descriptions-item label="Agent">{{ task.agent_id }}</el-descriptions-item>
          <el-descriptions-item label="服务器 SN">{{ task.server_sn || '-' }}</el-descriptions-item>
          <el-descriptions-item label="优先级">{{ task.priority }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatTime(task.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="开始时间">{{ formatTime(task.started_at) }}</el-descriptions-item>
          <el-descriptions-item label="结束时间">{{ formatTime(task.finished_at) }}</el-descriptions-item>
          <el-descriptions-item label="租约 ID">{{ task.lease_id || '-' }}</el-descriptions-item>
          <el-descriptions-item label="租约到期">{{ formatTime(task.lease_expires_at) }}</el-descriptions-item>
          <el-descriptions-item label="尝试次数">{{ task.attempts }}</el-descriptions-item>
          <el-descriptions-item label="错误码">{{ task.error_code || '-' }}</el-descriptions-item>
          <el-descriptions-item label="错误信息" :span="2">{{ task.error_message || '-' }}</el-descriptions-item>
        </el-descriptions>
      </div>

      <div v-if="hasProgress" class="rf-panel">
        <h3 class="rf-section-title">执行进度</h3>
        <el-progress
          v-if="progressPercent !== null"
          :percentage="progressPercent"
          :status="task.status === 'failed' ? 'exception' : undefined"
        />
        <p class="rf-muted" style="margin-top: 8px">
          {{ task.progress.message || '-' }}
          <span v-if="task.progress.done !== null && task.progress.done !== undefined">
            （{{ task.progress.done }} / {{ task.progress.total ?? '?' }}）
          </span>
        </p>
      </div>

      <div class="rf-grid-2">
        <div class="rf-panel">
          <h3 class="rf-section-title">任务事件</h3>
          <EventTimeline :items="task.events || []" />
        </div>
        <div class="rf-panel">
          <h3 class="rf-section-title">证据文件</h3>
          <el-table :data="task.evidence || []" size="small" border>
            <el-table-column prop="filename" label="文件" min-width="180" show-overflow-tooltip />
            <el-table-column prop="kind" label="类型" width="110" />
            <el-table-column prop="stage" label="阶段" width="100" />
            <el-table-column label="大小" width="100">
              <template #default="{ row }">{{ formatBytes(row.size) }}</template>
            </el-table-column>
            <el-table-column label="" width="80" align="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="download(row)">下载</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!(task.evidence || []).length" description="暂无已上传证据" :image-size="60" />
        </div>
      </div>

      <div class="rf-panel">
        <el-collapse>
          <el-collapse-item title="参数 (params)" name="params">
            <JsonPreview :value="task.params || {}" />
          </el-collapse-item>
          <el-collapse-item title="结果 (result)" name="result">
            <JsonPreview :value="task.result || {}" />
          </el-collapse-item>
        </el-collapse>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import JsonPreview from '../components/JsonPreview.vue'
import EventTimeline from '../components/EventTimeline.vue'
import { cancelTask, getTask } from '../api'
import { formatTime, usePolling } from '../composables/usePolling'

const route = useRoute()
const task = ref(null)

const { run, loading, error } = usePolling(async () => {
  const data = await getTask(route.params.id)
  task.value = data.task
}, 5000)

const isTerminal = (value) => ['finished', 'failed', 'cancelled', 'expired'].includes(value)

const hasProgress = computed(() => {
  const progress = task.value?.progress
  if (!progress) return false
  return Boolean(
    progress.message ||
      (progress.done !== null && progress.done !== undefined) ||
      (progress.total !== null && progress.total !== undefined)
  )
})

const progressPercent = computed(() => {
  const progress = task.value?.progress
  const done = Number(progress?.done)
  const total = Number(progress?.total)
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
})

const formatBytes = (value) => {
  const size = Number(value) || 0
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`
}

const download = (row) => {
  if (!row.download_url) return
  window.open(row.download_url, '_blank')
}

const copyJson = async () => {
  if (!task.value) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(task.value, null, 2))
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许写入剪贴板')
  }
}

const cancel = async () => {
  try {
    await ElMessageBox.confirm('取消该任务？运行中的任务由 agent 在命令边界协作中止。', '取消任务', {
      type: 'warning',
      confirmButtonText: '取消任务',
      cancelButtonText: '返回'
    })
  } catch {
    return
  }
  try {
    await cancelTask(task.value.task_id, 'operator cancelled from webplane')
    ElMessage.success('已发送取消请求')
    run()
  } catch (exc) {
    ElMessage.error(exc.message || '取消失败')
  }
}
</script>

<style scoped>
.rf-grid-2 {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 16px;
}

@media (max-width: 1100px) {
  .rf-grid-2 {
    grid-template-columns: 1fr;
  }
}
</style>
