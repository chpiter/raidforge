<template>
  <div class="rf-page">
    <PageHeader title="任务" subtitle="Agent 任务队列与执行结果">
      <template #actions>
        <el-button :loading="loading" @click="run">刷新</el-button>
        <el-button type="primary" @click="$router.push('/tasks/new')">新建任务</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <div class="rf-panel">
      <div class="rf-toolbar">
        <el-select v-model="agentId" placeholder="全部 Agent" clearable filterable style="width: 230px">
          <el-option
            v-for="agent in agents"
            :key="agent.agent_id"
            :label="`${agent.agent_id}${agent.server_sn ? ' · ' + agent.server_sn : ''}`"
            :value="agent.agent_id"
          />
        </el-select>
        <el-select v-model="type" placeholder="全部类型" clearable style="width: 180px">
          <el-option v-for="item in taskTypes" :key="item.type" :label="item.label" :value="item.type" />
        </el-select>
        <el-select v-model="status" placeholder="全部状态" clearable style="width: 140px">
          <el-option label="待派发" value="pending" />
          <el-option label="已派发" value="leased" />
          <el-option label="运行中" value="running" />
          <el-option label="已完成" value="finished" />
          <el-option label="失败" value="failed" />
          <el-option label="已取消" value="cancelled" />
          <el-option label="已过期" value="expired" />
        </el-select>
        <el-input v-model="query" placeholder="搜索任务 / SN" clearable style="width: 220px" />
        <div class="spacer" />
        <span class="rf-muted">共 {{ total }} 条</span>
      </div>

      <el-table :data="tasks" style="margin-top: 12px" @row-click="openTask">
        <el-table-column prop="task_id" label="任务" min-width="230">
          <template #default="{ row }"><span class="rf-link rf-mono">{{ row.task_id }}</span></template>
        </el-table-column>
        <el-table-column label="类型" width="160">
          <template #default="{ row }">{{ labelFor(row.type) }}</template>
        </el-table-column>
        <el-table-column prop="agent_id" label="Agent" min-width="160" show-overflow-tooltip />
        <el-table-column prop="server_sn" label="服务器 SN" min-width="140" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column prop="phase" label="阶段" width="110" />
        <el-table-column label="创建时间" width="185">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="证据" width="70" align="center">
          <template #default="{ row }">{{ row.evidence_count ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="" width="140" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openTask(row)">详情</el-button>
            <el-button
              v-if="!isTerminal(row.status)"
              link
              type="danger"
              @click.stop="cancel(row)"
            >
              取消
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="rf-toolbar" style="margin-top: 12px; justify-content: flex-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @current-change="run"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { cancelTask, getAgents, getTasks, getTaskTypes } from '../api'
import { formatTime, usePolling } from '../composables/usePolling'

const route = useRoute()
const router = useRouter()
const tasks = ref([])
const total = ref(0)
const agents = ref([])
const taskTypes = ref([])
const agentId = ref(String(route.query.agent_id || ''))
const type = ref(String(route.query.type || ''))
const status = ref('')
const query = ref('')
const page = ref(1)
const pageSize = ref(20)

const isTerminal = (value) => ['finished', 'failed', 'cancelled', 'expired'].includes(value)
const labelFor = (value) => taskTypes.value.find((item) => item.type === value)?.label || value

const { run, loading, error } = usePolling(async () => {
  const data = await getTasks({
    agent_id: agentId.value || undefined,
    type: type.value || undefined,
    status: status.value || undefined,
    q: query.value || undefined,
    page: page.value,
    page_size: pageSize.value
  })
  tasks.value = data.items || []
  total.value = data.total || 0
}, 5000)

onMounted(async () => {
  const [agentData, typeData] = await Promise.all([
    getAgents({ page: 1, page_size: 200 }),
    getTaskTypes()
  ])
  agents.value = agentData.items || []
  taskTypes.value = typeData.items || []
})

watch([agentId, type, status, query], () => {
  page.value = 1
  run()
})

const onSizeChange = () => {
  page.value = 1
  run()
}

const openTask = (row) => router.push(`/tasks/${row.task_id}`)

const cancel = async (row) => {
  try {
    await ElMessageBox.confirm(`取消任务 ${row.task_id}？运行中的任务由 agent 在命令边界协作中止。`, '取消任务', {
      type: 'warning',
      confirmButtonText: '取消任务',
      cancelButtonText: '返回'
    })
  } catch {
    return
  }
  try {
    await cancelTask(row.task_id, 'operator cancelled from webplane')
    ElMessage.success('已发送取消请求')
    run()
  } catch (exc) {
    ElMessage.error(exc.message || '取消失败')
  }
}
</script>
