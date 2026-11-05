<template>
  <div class="rf-page">
    <PageHeader title="服务器" subtitle="已注册的 RaidForge Agent 服务器">
      <template #actions>
        <el-button :loading="loading" @click="run">刷新</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <div class="rf-panel">
      <div class="rf-toolbar">
        <el-select v-model="status" placeholder="全部状态" clearable style="width: 150px">
          <el-option label="在线" value="online" />
          <el-option label="离线" value="offline" />
          <el-option label="未知" value="unknown" />
        </el-select>
        <el-input v-model="query" placeholder="搜索服务器 / SN / 型号" clearable style="width: 280px" />
        <div class="spacer" />
        <span class="rf-muted">共 {{ total }} 台</span>
      </div>

      <el-table :data="servers" style="margin-top: 12px" @row-click="openServer">
        <el-table-column label="状态" width="90">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column prop="agent_id" label="服务器" min-width="190" show-overflow-tooltip />
        <el-table-column prop="server_sn" label="序列号" min-width="150" />
        <el-table-column label="厂商 / 型号" min-width="180">
          <template #default="{ row }">{{ [row.vendor, row.model].filter(Boolean).join(' ') || '-' }}</template>
        </el-table-column>
        <el-table-column prop="hostname" label="主机名" width="140" show-overflow-tooltip />
        <el-table-column label="阵列卡" width="90" align="center">
          <template #default="{ row }">{{ row.capabilities?.controllers?.length || 0 }}</template>
        </el-table-column>
        <el-table-column prop="agent_version" label="版本" width="90" />
        <el-table-column label="最后心跳" width="110">
          <template #default="{ row }">{{ relativeTime(row.last_heartbeat_at) }}</template>
        </el-table-column>
        <el-table-column label="队列" width="80" align="center">
          <template #default="{ row }">{{ row.queue_depth }} / {{ row.queue_max || '-' }}</template>
        </el-table-column>
        <el-table-column label="当前任务" min-width="150">
          <template #default="{ row }">
            <span v-if="row.current_task?.task_id" class="rf-mono">{{ row.current_task.type || row.current_task.task_id }}</span>
            <span v-else class="rf-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="130" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openServer(row)">进入</el-button>
            <el-button
              v-if="row.status === 'offline'"
              link
              type="danger"
              @click.stop="remove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="rf-toolbar" style="margin-top: 12px; justify-content: flex-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="run"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { deleteAgent, getAgents } from '../api'
import { relativeTime, usePolling } from '../composables/usePolling'

const router = useRouter()
const servers = ref([])
const total = ref(0)
const status = ref('')
const query = ref('')
const page = ref(1)
const pageSize = ref(20)

const { run, loading, error } = usePolling(async () => {
  const data = await getAgents({
    status: status.value || undefined,
    q: query.value || undefined,
    page: page.value,
    page_size: pageSize.value
  })
  servers.value = data.items || []
  total.value = data.total || 0
}, 5000)

watch([status, query], () => {
  page.value = 1
  run()
})

const onSizeChange = () => {
  page.value = 1
  run()
}

const openServer = (row) => router.push(`/servers/${encodeURIComponent(row.agent_id)}`)

const remove = async (row) => {
  try {
    await ElMessageBox.confirm(
      `删除服务器 ${row.agent_id}（SN ${row.server_sn || '-'}）？删除后将从列表隐藏，历史任务与报告仍保留；该服务器重新上线后会自动恢复。`,
      '删除服务器',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await deleteAgent(row.agent_id)
    ElMessage.success('已删除')
    if (servers.value.length === 1 && page.value > 1) page.value -= 1
    run()
  } catch (exc) {
    ElMessage.error(exc.message || '删除失败')
  }
}
</script>
