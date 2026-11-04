<template>
  <div class="rf-page">
    <PageHeader :title="agent?.agent_id || '服务器详情'" :subtitle="agent ? `SN ${agent.server_sn || '-'}` : ''">
      <template #actions>
        <el-button @click="$router.push('/servers')">返回服务器</el-button>
        <el-button link @click="$router.push('/tasks')">任务</el-button>
        <el-button :loading="loading" @click="run">刷新</el-button>
        <el-button :disabled="!agent" @click="refreshHardware">刷新硬件</el-button>
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
          <el-descriptions-item label="服务器">{{ agent.agent_id }}</el-descriptions-item>
          <el-descriptions-item label="序列号">{{ agent.server_sn || '-' }}</el-descriptions-item>
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

      <div class="rf-panel">
        <div class="rf-toolbar">
          <h3 class="rf-section-title">阵列卡</h3>
          <div class="spacer" />
          <span class="rf-muted">点击阵列卡查看硬盘与 RAID</span>
        </div>
        <el-table :data="controllers" style="margin-top: 8px" @row-click="openController">
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.known ? 'success' : 'warning'" effect="plain" size="small">
                {{ row.known ? '已上报' : '仅快照' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="controller_id" label="控制器" min-width="180" show-overflow-tooltip />
          <el-table-column label="厂商 / 型号" min-width="170">
            <template #default="{ row }">{{ [row.vendor, row.model].filter(Boolean).join(' ') || '-' }}</template>
          </el-table-column>
          <el-table-column prop="firmware" label="固件" width="110" />
          <el-table-column label="工具" width="100">
            <template #default="{ row }">{{ toolLabel(row.tool) }}</template>
          </el-table-column>
          <el-table-column label="物理盘" width="90" align="center">
            <template #default="{ row }">{{ row.has_pds ? row.pd_count : '未采集' }}</template>
          </el-table-column>
          <el-table-column label="RAID" width="90" align="center">
            <template #default="{ row }">{{ row.has_vds ? row.vd_count : '未采集' }}</template>
          </el-table-column>
          <el-table-column label="数据时间" width="150">
            <template #default="{ row }">{{ dataTime(row) }}</template>
          </el-table-column>
          <el-table-column label="" width="80" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openController(row)">进入</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!controllers.length" description="尚无阵列卡信息" :image-size="70">
          <el-button type="primary" @click="refreshHardware">下发 refresh_hardware</el-button>
        </el-empty>
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
          <h3 class="rf-section-title" style="margin-top: 18px">启动介质 / 存储</h3>
          <JsonPreview :value="{ boot_media: agent.boot_media, storage: agent.storage }" />
        </div>
        <div class="rf-panel">
          <h3 class="rf-section-title">硬件信息</h3>
          <JsonPreview :value="agent.hardware || {}" />
        </div>
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
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import JsonPreview from '../components/JsonPreview.vue'
import { createTask, getAgent, getAgentControllers, getAgentTasks } from '../api'
import { formatTime, relativeTime, usePolling } from '../composables/usePolling'
import { useTaskWatch } from '../composables/useTaskWatch'

const route = useRoute()
const router = useRouter()
const agent = ref(null)
const controllers = ref([])
const recentTasks = ref([])
const { watchTasks } = useTaskWatch()

const { run, loading, error } = usePolling(async () => {
  const id = route.params.agentId
  const [detail, controllerData, tasks] = await Promise.all([
    getAgent(id),
    getAgentControllers(id),
    getAgentTasks(id, { page: 1, page_size: 8 })
  ])
  agent.value = detail.agent
  controllers.value = controllerData.controllers || []
  recentTasks.value = tasks.items || []
}, 5000)

const toolLabel = (tool) => {
  if (!tool) return '-'
  if (typeof tool === 'string') return tool
  return tool.name || tool.path || '-'
}

const dataTime = (row) => {
  const value = row.pds_collected_at || row.vds_collected_at
  return value ? relativeTime(value) : '未采集'
}

const newTask = () =>
  router.push({ path: '/tasks/new', query: { agent_id: agent.value?.agent_id || '' } })

const openTask = (row) => router.push(`/tasks/${row.task_id}`)

const openController = (row) =>
  router.push(
    `/servers/${encodeURIComponent(agent.value.agent_id)}/controllers/${encodeURIComponent(row.controller_id)}`
  )

const refreshHardware = async () => {
  if (!agent.value) return
  try {
    const data = await createTask({
      agent_id: agent.value.agent_id,
      type: 'refresh_hardware',
      params: { controller_ids: null, include_raw_output: false },
      require_local_confirm: false
    })
    ElMessage.success('已下发刷新硬件任务，后台执行中')
    watchTasks([data.task.task_id], {
      title: '刷新服务器硬件',
      failureHint: '保留当前数据',
      onFinished: ({ anyFailed }) => {
        if (!anyFailed) run()
      }
    })
  } catch (exc) {
    ElMessage.error(exc.message || '下发刷新任务失败')
  }
}
</script>

<style scoped>
.rf-grid-2 {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
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
