<template>
  <div class="rf-page">
    <PageHeader
      :title="session ? `证据 ${session.session_id}` : '证据详情'"
      subtitle="HTTP 上传的擦除会话（只读）"
    >
      <template #actions>
        <el-button @click="backToList">返回列表</el-button>
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button v-if="session" @click="downloadArchive">下载归档</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <template v-if="session">
      <div class="rf-panel">
        <el-descriptions :column="3" border>
          <el-descriptions-item label="服务器 SN">{{ session.server_sn || '-' }}</el-descriptions-item>
          <el-descriptions-item label="接收时间">{{ formatTime(session.received_at) }}</el-descriptions-item>
          <el-descriptions-item label="大小">{{ formatBytes(session.bytes) }}</el-descriptions-item>
          <el-descriptions-item label="事件数">{{ session.events }}</el-descriptions-item>
          <el-descriptions-item label="文件数">{{ session.file_count }}</el-descriptions-item>
          <el-descriptions-item label="SHA256">
            <span class="rf-mono">{{ shortSha }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="rf-panel">
        <el-tabs v-model="activeTab">
          <el-tab-pane label="报告" name="report">
            <div class="rf-toolbar" style="margin-bottom: 10px">
              <el-button v-if="session.report" type="primary" @click="downloadReport">
                下载 DOCX
              </el-button>
              <span class="rf-muted">{{ session.report?.name || '无报告' }}</span>
            </div>
            <el-alert
              v-if="reportError"
              type="warning"
              :closable="false"
              show-icon
              :title="reportError.message"
            />
            <div v-if="report" class="rf-report" v-html="report.html"></div>
            <el-empty
              v-else-if="!reportLoading && !reportError"
              description="该会话没有 DOCX 报告"
              :image-size="70"
            />
          </el-tab-pane>

          <el-tab-pane label="文件" name="files">
            <div class="evidence-files">
              <el-table
                ref="fileTable"
                :data="session.files"
                size="small"
                border
                highlight-current-row
                @row-click="previewFile"
              >
                <el-table-column prop="path" label="文件" min-width="220" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span>{{ row.path }}</span>
                    <el-tag
                      v-if="selectedFile === row.path"
                      size="small"
                      effect="plain"
                      style="margin-left: 6px"
                    >
                      预览中
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="类型" width="100">
                  <template #default="{ row }">{{ row.is_report ? '报告' : '文件' }}</template>
                </el-table-column>
                <el-table-column label="大小" width="100">
                  <template #default="{ row }">{{ formatBytes(row.size) }}</template>
                </el-table-column>
                <el-table-column label="" width="130" align="right">
                  <template #default="{ row }">
                    <el-button link type="primary" @click.stop="previewFile(row)">预览</el-button>
                    <el-button link @click.stop="downloadFile(row)">下载</el-button>
                  </template>
                </el-table-column>
              </el-table>

              <div class="rf-panel preview-panel">
                <h3 class="rf-section-title">预览</h3>
                <template v-if="preview.type === 'image'">
                  <p class="rf-muted">{{ preview.name }}</p>
                  <img :src="preview.url" :alt="preview.name" class="preview-image" />
                </template>
                <template v-else-if="preview.type === 'text'">
                  <p class="rf-muted">{{ preview.name }}</p>
                  <pre class="rf-json preview-text">{{ preview.text }}</pre>
                </template>
                <el-empty v-else description="点击左侧文件进行预览" :image-size="60" />
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="截图" name="snapshots">
            <div v-if="snapshots.length" class="snapshot-grid">
              <div
                v-for="(file, index) in snapshots"
                :key="file.path"
                class="snapshot-card"
              >
                <el-image
                  :src="fileUrl(file.path)"
                  :preview-src-list="snapshotUrls"
                  :initial-index="index"
                  fit="contain"
                  class="snapshot-image"
                />
                <span class="rf-muted">{{ file.name }}</span>
              </div>
            </div>
            <el-empty v-else description="该会话没有截图" :image-size="70" />
          </el-tab-pane>

          <el-tab-pane label="事件" name="events">
            <EventTimeline :items="events" />
          </el-tab-pane>

          <el-tab-pane label="清单" name="manifest">
            <JsonPreview :value="session.manifest" />
          </el-tab-pane>
        </el-tabs>
      </div>
    </template>

    <el-empty v-else-if="!loading" description="会话不存在" :image-size="70" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import JsonPreview from '../components/JsonPreview.vue'
import EventTimeline from '../components/EventTimeline.vue'
import {
  getSession,
  getSessionEvents,
  getSessionFileText,
  getSessionReport,
  sessionDownloadUrl,
  sessionFileUrl
} from '../api'
import { formatBytes, formatTime } from '../composables/usePolling'

const route = useRoute()
const router = useRouter()
const sessionId = computed(() => String(route.params.sessionId || ''))
const session = ref(null)
const fileTable = ref(null)
const selectedFile = ref('')
const events = ref([])
const report = ref(null)
const reportError = ref(null)
const reportLoading = ref(false)
const activeTab = ref('report')
const preview = ref({ type: '', name: '', url: '', text: '' })
const loading = ref(false)
const error = ref(null)

const IMAGE_SUFFIXES = ['.svg', '.png', '.jpg', '.jpeg']
const TEXT_SUFFIXES = ['.json', '.jsonl', '.txt', '.log', '.csv', '.cast']

const shortSha = computed(() => {
  const value = session.value?.sha256 || ''
  return value ? `${value.slice(0, 16)}…` : '-'
})

const snapshots = computed(() =>
  (session.value?.files || []).filter(
    (file) => file.path.startsWith('snapshots/') && isImage(file.path)
  )
)

const snapshotUrls = computed(() => snapshots.value.map((file) => fileUrl(file.path)))

const suffixOf = (path) => {
  const index = String(path || '').lastIndexOf('.')
  return index >= 0 ? String(path).slice(index).toLowerCase() : ''
}

const isImage = (path) => IMAGE_SUFFIXES.includes(suffixOf(path))
const isText = (path) => TEXT_SUFFIXES.includes(suffixOf(path))
const fileUrl = (path) => sessionFileUrl(sessionId.value, path)

const loadReport = async () => {
  report.value = null
  reportError.value = null
  if (!session.value?.report) return
  reportLoading.value = true
  try {
    report.value = await getSessionReport(sessionId.value)
  } catch (exc) {
    reportError.value = exc
  } finally {
    reportLoading.value = false
  }
}

const loadEvents = async () => {
  try {
    const data = await getSessionEvents(sessionId.value)
    events.value = data.items || []
  } catch {
    events.value = []
  }
}

const load = async () => {
  loading.value = true
  try {
    const data = await getSession(sessionId.value)
    session.value = data.session
    if (!session.value?.report) activeTab.value = 'files'
    error.value = null
    await Promise.all([loadReport(), loadEvents()])
  } catch (exc) {
    error.value = exc
    session.value = null
  } finally {
    loading.value = false
  }
}

const previewFile = async (row) => {
  selectedFile.value = row.path
  fileTable.value?.setCurrentRow(row)
  if (row.is_report || suffixOf(row.path) === '.docx') {
    activeTab.value = 'report'
    return
  }
  if (isImage(row.path)) {
    preview.value = { type: 'image', name: row.path, url: fileUrl(row.path), text: '' }
    return
  }
  if (isText(row.path)) {
    try {
      const text = await getSessionFileText(sessionId.value, row.path)
      preview.value = { type: 'text', name: row.path, url: '', text }
    } catch (exc) {
      ElMessage.error(exc.message || '读取文件失败')
    }
    return
  }
  ElMessage.info('该类型不支持内联预览，请下载原文件')
}

const downloadFile = (row) => {
  window.open(sessionFileUrl(sessionId.value, row.path, true), '_blank')
}

const downloadReport = () => {
  if (!session.value?.report) return
  window.open(sessionFileUrl(sessionId.value, session.value.report.path, true), '_blank')
}

const downloadArchive = () => {
  window.open(sessionDownloadUrl(sessionId.value), '_blank')
}

const backToList = () => router.push('/evidence')

onMounted(load)
</script>

<style scoped>
.evidence-files {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr);
  gap: 16px;
}

.preview-panel {
  min-height: 260px;
  overflow: auto;
}

.preview-image {
  max-width: 100%;
  max-height: 420px;
  display: block;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #f8fafc;
}

.preview-text {
  max-height: 420px;
  overflow: auto;
}

.snapshot-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 12px;
}

.snapshot-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}

.snapshot-image {
  width: 100%;
  height: 140px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #f8fafc;
}

:deep(.rf-report) {
  line-height: 1.65;
  color: #1f2937;
}

:deep(.rf-report h1),
:deep(.rf-report h2),
:deep(.rf-report h3) {
  margin: 18px 0 8px;
}

:deep(.rf-report p) {
  margin: 6px 0;
}

:deep(.rf-report-table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 13px;
}

:deep(.rf-report-table td) {
  border: 1px solid #d8e0ea;
  padding: 6px 8px;
  vertical-align: top;
}

:deep(.rf-report-note),
:deep(.rf-report-subtitle) {
  color: #6b7a90;
  font-size: 12px;
}

@media (max-width: 1000px) {
  .evidence-files {
    grid-template-columns: 1fr;
  }
}
</style>
