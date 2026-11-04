<template>
  <div class="rf-page">
    <PageHeader :title="agent?.agent_id || 'Agent 详情'" :subtitle="agent ? `SN ${agent.server_sn || '-'}` : ''">
      <template #actions>
        <el-button @click="$router.push('/agents')">返回列表</el-button>
        <el-button :loading="loading" @click="run">刷新</el-button>
        <el-button type="primary" :disabled="!agent" @click="newTask">新建任务</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <template v-if="agent">
      <div class="rf-panel">
        <div class="rf-toolbar" style="margin-bottom: 12px">
          <StatusTag :status="agent.status" />
          <span class="rf-muted">最后心跳 {{ relativeTime(agent.last_heartbeat_at) }}</span>
          <div class="spacer" />
          <el-tag v-for="(count, key) in agent.task_counts" :key="key" effect="plain" size="small">
            {{ key }}: {{ count }}
          </el-tag>
        </div>
        <el-descriptions :column="3" border>
          <el-descriptions-item label="Agent ID">{{ agent.agent_id }}</el-descriptions-item>
          <el-descriptions-item label="服务器 SN">{{ agent.server_sn || '-' }}</el-descriptions-item>
          <el-descriptions-item label="主机名">{{ agent.hostname || '-' }}</el-descriptions-item>
          <el-descriptions-item label="厂商 / 型号">
            {{ [agent.vendor, agent.model].filter(Boolean).join(' ') || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="Agent 版本">{{ agent.agent_version || '-' }}</el-descriptions-item>
          <el-descriptions-item label="协议版本">{{ agent.protocol_version }}</el-descriptions-item>
          <el-descriptions-item label="最后心跳">{{ formatTime(agent.last_heartbeat_at) }}</el-descriptions-item>
          <el-descriptions-item label="来源 IP">{{ agent.last_seen_ip || '-' }}</el-descriptions-item>
          <el-descriptions-item label="队列">
            {{ agent.queue_depth }} / {{ agent.queue_max || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="最近错误" :span="3">
            {{ agent.last_error || '-' }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="rf-grid-2">
        <div class="rf-panel">
          <h3 class="rf-section-title">能力</h3>
          <div class="tag-wrap">
            <el-tag
              v-for="taskType in agent.capabilities?.task_types || []"
              :key="taskType"
              effect="plain"
              size="small"
            >
              {{ taskType }}
            </el-tag>
            <span v-if="!(agent.capabilities?.task_types || []).length" class="rf-muted">未上报</span>
          </div>
          <h3 class="rf-section-title" style="margin-top: 18px">控制器</h3>
          <el-table :data="controllers" size="small" border>
            <el-table-column prop="controller_id" label="ID" min-width="160" />
            <el-table-column prop="vendor" label="厂商" width="90" />
            <el-table-column prop="model" label="型号" min-width="140" />
            <el-table-column prop="tool" label="工具" width="100" />
            <el-table-column prop="firmware" label="固件" width="120" />
          </el-table>
          <el-empty v-if="!controllers.length" description="尚未上报控制器" :image-size="60" />
        </div>

        <div class="rf-panel">
          <h3 class="rf-section-title">启动介质 / 存储</h3>
          <JsonPreview :value="{ boot_media: agent.boot_media, storage: agent.storage }" />
        </div>
      </div>

      <div class="rf-panel">
        <h3 class="rf-section-title">硬件信息</h3>
        <JsonPreview :value="agent.hardware || {}" />
      </div>

      <div class="rf-panel">
        <div class="rf-toolbar">
          <h3 class="rf-section-title">最近任务</h3>
          <div class="spacer" />
          <el-button link type="primary" @click="$router.push(`/tasks?agent_id=${encodeURIComponent(agent.agent_id)}`)">
            查看全部
          </el-button>
        </div>
        <el-table :data="recentTasks" size="small" @row-click="openTask">
          <el-table-column prop="task_id" label="任务" min-width="220">
            <template #default="{ row }"><span class="rf-link rf-mono">{{ row.task_id }}</span></template>
          </el-table-column>
          <el-table-column prop="type" label="类型" width="150" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }"><StatusTag :status="row.status" /></template>
          </el-table-column>
          <el-table-column label="创建时间" width="190">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import JsonPreview from '../components/JsonPreview.vue'
import { getAgent, getAgentTasks } from '../api'
import { formatTime, relativeTime, usePolling } from '../composables/usePolling'

const route = useRoute()
const router = useRouter()
const agent = ref(null)
const recentTasks = ref([])

const { run, loading, error } = usePolling(async () => {
  const id = route.params.id
  const [detail, tasks] = await Promise.all([
    getAgent(id),
    getAgentTasks(id, { page: 1, page_size: 8 })
  ])
  agent.value = detail.agent
  recentTasks.value = tasks.items || []
}, 5000)

const controllers = computed(() => agent.value?.capabilities?.controllers || [])

const newTask = () =>
  router.push({ path: '/tasks/new', query: { agent_id: agent.value?.agent_id || '' } })

const openTask = (row) => router.push(`/tasks/${row.task_id}`)
</script>

<style scoped>
.rf-grid-2 {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 16px;
}

.tag-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

@media (max-width: 1100px) {
  .rf-grid-2 {
    grid-template-columns: 1fr;
  }
}
</style>
