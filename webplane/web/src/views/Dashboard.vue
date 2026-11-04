<template>
  <div class="rf-page">
    <PageHeader title="概览" subtitle="RaidForge Agent 在线状态与最近任务">
      <template #actions>
        <span class="rf-muted rf-mono">更新于 {{ lastText }}</span>
        <el-button :loading="loading" @click="run">刷新</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <div class="rf-grid-4">
      <StatCard label="在线 Agent" :value="onlineText" hint="心跳正常" tone="success" />
      <StatCard label="待派发" :value="stats?.tasks?.pending ?? '-'" hint="等待 agent 拉取" tone="warning" />
      <StatCard label="运行中" :value="runningText" hint="含已派发" tone="primary" />
      <StatCard label="24h 失败" :value="stats?.tasks?.failed_24h ?? '-'" hint="需要关注" tone="danger" />
    </div>

    <div class="rf-panel">
      <div class="rf-toolbar">
        <h3 class="rf-section-title">最近任务</h3>
        <div class="spacer" />
        <el-button link type="primary" @click="$router.push('/tasks')">查看全部</el-button>
      </div>
      <el-table :data="tasks" size="default" @row-click="openTask">
        <el-table-column prop="task_id" label="任务" min-width="230">
          <template #default="{ row }">
            <span class="rf-link rf-mono">{{ row.task_id }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="150" />
        <el-table-column prop="agent_id" label="Agent" min-width="170" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column label="创建时间" width="190">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="证据" width="80" align="center">
          <template #default="{ row }">{{ row.evidence_count ?? 0 }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import StatusTag from '../components/StatusTag.vue'
import { getStats, getTasks } from '../api'
import { formatTime, usePolling } from '../composables/usePolling'

const router = useRouter()
const stats = ref(null)
const tasks = ref([])

const { run, loading, error, lastUpdated } = usePolling(async () => {
  const [statsData, taskData] = await Promise.all([
    getStats(),
    getTasks({ page: 1, page_size: 8 })
  ])
  stats.value = statsData
  tasks.value = taskData.items || []
}, 5000)

const onlineText = computed(() => {
  if (!stats.value) return '-'
  return `${stats.value.agents.online} / ${stats.value.agents.total}`
})

const runningText = computed(() => {
  if (!stats.value) return '-'
  return `${stats.value.tasks.running} (+${stats.value.tasks.leased})`
})

const lastText = computed(() =>
  lastUpdated.value ? lastUpdated.value.toLocaleTimeString() : '-'
)

const openTask = (row) => router.push(`/tasks/${row.task_id}`)
</script>
