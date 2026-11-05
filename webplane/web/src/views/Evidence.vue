<template>
  <div class="rf-page">
    <PageHeader
      title="证据与报告"
      subtitle="通过 HTTP 上传到 webplane 的擦除会话；硬盘擦除仅允许在本地 TUI 执行。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <div class="rf-panel">
      <div class="rf-toolbar">
        <el-input
          v-model="query"
          placeholder="搜索会话 ID / 服务器 SN / 最后事件"
          clearable
          style="width: 320px"
          @keyup.enter="search"
        />
        <el-button @click="search">搜索</el-button>
        <div class="spacer" />
        <span class="rf-muted">共 {{ total }} 个会话</span>
      </div>

      <el-table :data="items" style="margin-top: 12px" @row-click="open">
        <el-table-column prop="session_id" label="会话 ID" min-width="230" show-overflow-tooltip />
        <el-table-column prop="server_sn" label="服务器 SN" min-width="150" />
        <el-table-column label="接收时间" width="170">
          <template #default="{ row }">{{ formatTime(row.received_at) }}</template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatBytes(row.bytes) }}</template>
        </el-table-column>
        <el-table-column prop="events" label="事件" width="80" align="center" />
        <el-table-column prop="file_count" label="文件" width="80" align="center" />
        <el-table-column prop="last_event" label="最后事件" min-width="160" show-overflow-tooltip />
        <el-table-column label="报告" width="90" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.report_file" size="small" type="success" effect="plain">DOCX</el-tag>
            <span v-else class="rf-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="80" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="open(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && !items.length" description="暂无 HTTP 上传的会话" :image-size="70" />

      <div class="rf-toolbar" style="margin-top: 12px; justify-content: flex-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '../components/PageHeader.vue'
import { getSessions } from '../api'
import { formatBytes, formatTime } from '../composables/usePolling'

const router = useRouter()
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const query = ref('')
const loading = ref(false)
const error = ref(null)

const load = async () => {
  loading.value = true
  try {
    const data = await getSessions({
      page: page.value,
      page_size: pageSize.value,
      q: query.value.trim() || undefined
    })
    items.value = data.items || []
    total.value = data.total || 0
    error.value = null
  } catch (exc) {
    error.value = exc
  } finally {
    loading.value = false
  }
}

const search = () => {
  page.value = 1
  load()
}

const onSizeChange = () => {
  page.value = 1
  load()
}

const open = (row) => {
  router.push(`/evidence/${encodeURIComponent(row.session_id)}`)
}

onMounted(load)
</script>
