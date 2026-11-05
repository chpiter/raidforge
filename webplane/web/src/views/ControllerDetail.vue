<template>
  <div class="rf-page">
    <PageHeader :title="controllerId" :subtitle="subtitle">
      <template #actions>
        <el-button @click="back">返回服务器</el-button>
        <el-button :loading="loading" @click="run">刷新页面</el-button>
        <el-button @click="refreshTopology(['list_pds', 'list_vds'])">刷新数据</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error.message" />

    <template v-if="detail">
      <el-alert
        v-if="detail.controller.known === false"
        type="warning"
        :closable="false"
        show-icon
        title="心跳未上报该控制器"
        description="当前控制器只存在于历史快照中，可能已更换或心跳异常。"
      />
      <el-alert
        v-if="detail.stale"
        type="warning"
        :closable="false"
        show-icon
        title="数据已超过 15 分钟"
        description="写操作前请先刷新硬盘 / RAID；agent 仍会重新扫描并校验。"
      />
      <el-alert
        v-if="!detail.has_pds || !detail.has_vds"
        type="info"
        :closable="false"
        show-icon
        title="尚未完整采集该阵列卡"
        description="首次打开会自动下发 list_pds + list_vds；也可以使用刷新按钮。"
      />

      <div class="rf-panel">
        <div class="rf-toolbar" style="margin-bottom: 12px">
          <el-tag :type="detail.controller.known ? 'success' : 'warning'" effect="plain">
            {{ detail.controller.known ? '已上报' : '仅快照' }}
          </el-tag>
          <el-tag effect="plain">{{ detail.controller.vendor || '-' }}</el-tag>
          <span class="rf-muted">固件 {{ detail.controller.firmware || '-' }}</span>
          <div class="spacer" />
          <span class="rf-muted">topology {{ hashPrefix }}</span>
        </div>
        <el-descriptions :column="3" border>
          <el-descriptions-item label="控制器">{{ detail.controller.controller_id }}</el-descriptions-item>
          <el-descriptions-item label="厂商 / 型号">
            {{ [detail.controller.vendor, detail.controller.model].filter(Boolean).join(' ') || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="序列号">{{ detail.controller.serial || '-' }}</el-descriptions-item>
          <el-descriptions-item label="固件">{{ detail.controller.firmware || '-' }}</el-descriptions-item>
          <el-descriptions-item label="工具">{{ toolLabel }}</el-descriptions-item>
          <el-descriptions-item label="索引">{{ detail.controller.index }}</el-descriptions-item>
          <el-descriptions-item label="硬盘采集">{{ timeText(detail.pds_collected_at) }}</el-descriptions-item>
          <el-descriptions-item label="RAID 采集">{{ timeText(detail.vds_collected_at) }}</el-descriptions-item>
          <el-descriptions-item label="物理盘 / RAID">
            {{ detail.pds.length }} / {{ detail.vds.length }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="rf-panel">
        <el-tabs v-model="activeTab">
          <el-tab-pane label="硬盘" name="disks">
            <div class="rf-toolbar" style="margin-bottom: 10px">
              <el-button :disabled="!selectedDisks.length" @click="openCreateRaid(selectedDisks)">
                用选中盘建 RAID
              </el-button>
              <div class="spacer" />
              <el-button @click="refreshTopology(['list_pds'])">刷新硬盘</el-button>
            </div>
            <el-table
              ref="diskTable"
              :data="detail.pds"
              :row-key="keyOf"
              border
              @selection-change="onSelectionChange"
            >
              <el-table-column type="selection" width="46" :selectable="selectable" reserve-selection />
              <el-table-column prop="serial" label="Serial" min-width="140" />
              <el-table-column prop="wwn" label="WWN" min-width="190" class-name="rf-mono" />
              <el-table-column label="容量" width="110">
                <template #default="{ row }">{{ formatBytes(row.expected_size_bytes) }}</template>
              </el-table-column>
              <el-table-column prop="media_type" label="介质" width="85" />
              <el-table-column prop="state" label="状态" min-width="150" />
              <el-table-column label="RAID 成员" width="110">
                <template #default="{ row }">{{ row.raid_member_of || '-' }}</template>
              </el-table-column>
              <el-table-column label="启动保护" width="100">
                <template #default="{ row }">
                  <el-tag v-if="row.boot_protected" type="danger" size="small">保护</el-tag>
                  <span v-else class="rf-muted">-</span>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-if="!detail.pds.length" description="尚未采集硬盘，正在自动刷新或请手动刷新" :image-size="70" />
          </el-tab-pane>

          <el-tab-pane label="RAID" name="raids">
            <div class="rf-toolbar" style="margin-bottom: 10px">
              <el-button type="primary" @click="openCreateRaid([])">创建 RAID</el-button>
              <el-button type="danger" :disabled="!detail.has_vds" @click="clearVisible = true">
                清除控制器配置
              </el-button>
              <div class="spacer" />
              <el-button @click="refreshTopology(['list_vds'])">刷新 RAID</el-button>
            </div>
            <el-table :data="detail.vds" border>
              <el-table-column prop="vd_id" label="VD ID" width="100" />
              <el-table-column prop="vd_name" label="名称" min-width="140" />
              <el-table-column prop="raid_level" label="级别" width="110" />
              <el-table-column label="容量" width="120">
                <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
              </el-table-column>
              <el-table-column prop="state" label="状态" min-width="130" />
              <el-table-column label="成员" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ membersText(row) }}</template>
              </el-table-column>
              <el-table-column label="" width="90" align="right">
                <template #default="{ row }">
                  <el-button link type="danger" @click="openDelete(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-if="!detail.vds.length" description="尚无 RAID 卷，或尚未采集" :image-size="70" />
          </el-tab-pane>
        </el-tabs>
      </div>
    </template>

    <CreateRaidDialog
      v-model="createRaidVisible"
      :agent-id="agentId"
      :controller-id="controllerId"
      :disks="detail?.pds || []"
      :preselected="createRaidPreselected"
      @submitted="onSubmitted"
    />
    <DeleteVdDialog
      v-model="deleteVisible"
      :agent-id="agentId"
      :controller-id="controllerId"
      :vd="activeVd"
      @submitted="onSubmitted"
    />
    <ClearConfigDialog
      v-model="clearVisible"
      :agent-id="agentId"
      :controller-id="controllerId"
      :vd-count="(detail?.vds || []).length"
      :vds-collected="Boolean(detail?.has_vds)"
      @submitted="onSubmitted"
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import CreateRaidDialog from '../components/dialogs/CreateRaidDialog.vue'
import DeleteVdDialog from '../components/dialogs/DeleteVdDialog.vue'
import ClearConfigDialog from '../components/dialogs/ClearConfigDialog.vue'
import { createTasks, getAgentController } from '../api'
import { formatBytes, formatTime, usePolling } from '../composables/usePolling'
import { useTaskWatch } from '../composables/useTaskWatch'

const autoRefreshed = new Set()

const route = useRoute()
const router = useRouter()
const agentId = computed(() => String(route.params.agentId || ''))
const controllerId = computed(() => String(route.params.controllerId || ''))

const detail = ref(null)
const activeTab = ref('disks')
const selectedDisks = ref([])
const createRaidVisible = ref(false)
const createRaidPreselected = ref([])
const deleteVisible = ref(false)
const clearVisible = ref(false)
const activeVd = ref(null)
const { watchTasks } = useTaskWatch()
let autoNoticeShown = false

const { run, loading, error } = usePolling(async () => {
  const data = await getAgentController(agentId.value, controllerId.value)
  detail.value = data
  const key = `${agentId.value}:${controllerId.value}`
  if (!autoRefreshed.has(key) && (!data.has_pds || !data.has_vds)) {
    autoRefreshed.add(key)
    await refreshTopology(['list_pds', 'list_vds'], { auto: true })
  }
}, 5000)

const subtitle = computed(() => {
  const controller = detail.value?.controller
  if (!controller) return ''
  return [controller.vendor, controller.model, controller.serial].filter(Boolean).join(' · ')
})

const toolLabel = computed(() => {
  const tool = detail.value?.controller?.tool
  if (!tool) return '-'
  return typeof tool === 'string' ? tool : tool.name || '-'
})

const hashPrefix = computed(() => {
  const value = detail.value?.topology_hash || ''
  return value ? value.slice(0, 12) : '-'
})

const keyOf = (row) => row?.wwn || row?.serial || ''
const selectable = (row) =>
  Boolean(row?.serial) &&
  /^0x[0-9a-f]{16}$/i.test(row?.wwn || '') &&
  !row?.boot_protected

const onSelectionChange = (rows) => {
  selectedDisks.value = rows
}

const timeText = (value) => (value ? formatTime(value) : '未采集')

const membersText = (vd) => {
  if (Array.isArray(vd.member_serials) && vd.member_serials.length) {
    return vd.member_serials.join(', ')
  }
  if (Array.isArray(vd.member_disk_ids) && vd.member_disk_ids.length) {
    return `${vd.member_disk_ids.length} 块`
  }
  return '-'
}

function openDrawer(taskIds, title, options = {}) {
  watchTasks(taskIds, {
    title: title || '任务',
    failureHint: '保留当前数据',
    silent: Boolean(options.silent),
    onFinished: ({ anyFailed }) => {
      if (!anyFailed) run()
    }
  })
}

async function refreshTopology(types, options = {}) {
  if (!agentId.value || !controllerId.value) return
  try {
    const payloads = types.map((type) => ({
      agent_id: agentId.value,
      type,
      params: { controller_ids: [controllerId.value], include_raw_output: false },
      require_local_confirm: false
    }))
    const results = await createTasks(payloads)
    const ids = results.map((item) => item.task.task_id)
    if (options.auto) {
      if (!autoNoticeShown) {
        autoNoticeShown = true
        ElMessage.info('首次采集已下发，后台执行中')
      }
    } else {
      ElMessage.success('刷新任务已下发，后台执行中')
    }
    openDrawer(
      ids,
      types.length > 1 ? '刷新阵列卡数据' : `刷新 ${types[0]}`,
      { silent: Boolean(options.auto) }
    )
  } catch (exc) {
    const detailText = (exc.fields || []).map((item) => `${item.field}: ${item.message}`).join('；')
    ElMessage.error(detailText ? `${exc.message}: ${detailText}` : exc.message || '下发刷新任务失败')
  }
}

const onSubmitted = (taskIds) => {
  ElMessage.success('任务已下发，后台执行中')
  openDrawer(taskIds, '执行任务')
}

const openCreateRaid = (preselected) => {
  createRaidPreselected.value = [...(preselected || [])]
  createRaidVisible.value = true
}

const openDelete = (vd) => {
  activeVd.value = vd
  deleteVisible.value = true
}

const back = () => router.push(`/servers/${encodeURIComponent(agentId.value)}`)
</script>
