<template>
  <el-dialog
    :model-value="modelValue"
    title="创建 RAID"
    width="820px"
    :close-on-click-modal="false"
    @update:model-value="onVisible"
  >
    <el-alert
      type="error"
      :closable="false"
      show-icon
      title="破坏性操作"
      description="创建 RAID 会占用所选磁盘。默认仅预演；关闭 dry_run 前请再次核对目标。"
    />

    <el-form label-position="top" style="margin-top: 12px">
      <el-row :gutter="16">
        <el-col :span="6">
          <el-form-item label="卷名称" required>
            <el-input v-model="name" maxlength="15" show-word-limit placeholder="vd-name" />
          </el-form-item>
        </el-col>
        <el-col :span="6">
          <el-form-item label="RAID 级别">
            <el-select v-model="level" style="width: 100%">
              <el-option label="RAID0" value="RAID0" />
              <el-option label="RAID1" value="RAID1" />
              <el-option label="RAID5" value="RAID5" />
              <el-option label="RAID10" value="RAID10" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="6">
          <el-form-item label="条带 (KB)">
            <el-input-number
              v-model="stripKb"
              :min="16"
              :max="4096"
              :value-on-clear="null"
              controls-position="right"
              placeholder="留空=控制器默认"
              style="width: 100%"
            />
          </el-form-item>
        </el-col>
        <el-col :span="6">
          <el-form-item label="初始化方式">
            <el-select v-model="init" style="width: 100%">
              <el-option label="快速初始化 (fast)" value="fast" />
              <el-option label="不显式指定 (none)" value="none" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      :title="`已选 ${selectedRows.length} 块，预计容量 ${formatBytes(capacity)}`"
      :description="ruleText"
      style="margin-bottom: 10px"
    />

    <el-table
      ref="tableRef"
      :data="availableDisks"
      :row-key="keyOf"
      size="small"
      border
      max-height="320"
      @selection-change="onSelectionChange"
    >
      <el-table-column type="selection" width="46" :selectable="selectable" reserve-selection />
      <el-table-column prop="serial" label="Serial" min-width="140" />
      <el-table-column prop="wwn" label="WWN" min-width="180" class-name="rf-mono" />
      <el-table-column label="容量" width="110">
        <template #default="{ row }">{{ formatBytes(row.expected_size_bytes) }}</template>
      </el-table-column>
      <el-table-column prop="media_type" label="介质" width="90" />
      <el-table-column prop="state" label="状态" width="140" />
    </el-table>

    <el-form label-position="top" style="margin-top: 12px">
      <el-form-item label="仅预演（dry_run）">
        <el-switch v-model="dryRun" />
      </el-form-item>
      <el-checkbox v-if="!dryRun" v-model="confirmed">
        我已确认目标磁盘与 RAID 级别
      </el-checkbox>
    </el-form>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="danger" :loading="submitting" :disabled="!canSubmit" @click="submit">
        下发创建任务
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { createTask } from '../../api'
import { formatBytes } from '../../composables/usePolling'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agentId: { type: String, required: true },
  controllerId: { type: String, required: true },
  disks: { type: Array, default: () => [] },
  preselected: { type: Array, default: () => [] }
})
const emit = defineEmits(['update:modelValue', 'submitted'])

const name = ref('')
const level = ref('RAID0')
const stripKb = ref(null)
const init = ref('fast')
const dryRun = ref(true)
const confirmed = ref(false)
const submitting = ref(false)
const error = ref('')
const selectedRows = ref([])
const tableRef = ref(null)

const keyOf = (row) => row?.wwn || row?.serial || ''
const isUsable = (row) =>
  Boolean(row?.serial) &&
  /^0x[0-9a-f]{16}$/i.test(row?.wwn || '') &&
  !row?.boot_protected &&
  !row?.raid_member_of

const availableDisks = computed(() => props.disks.filter(isUsable))
const selectable = (row) => isUsable(row)

const ruleText = computed(() => {
  if (level.value === 'RAID0') return 'RAID0：至少 1 块盘'
  if (level.value === 'RAID1') return 'RAID1：恰好 2 块盘'
  if (level.value === 'RAID5') return 'RAID5：至少 3 块盘'
  return 'RAID10：至少 4 块且为偶数'
})

const countValid = computed(() => {
  const count = selectedRows.value.length
  if (level.value === 'RAID0') return count >= 1
  if (level.value === 'RAID1') return count === 2
  if (level.value === 'RAID5') return count >= 3
  return count >= 4 && count % 2 === 0
})

const capacity = computed(() => {
  const rows = selectedRows.value
  if (!rows.length) return 0
  const minimum = Math.min(...rows.map((row) => Number(row.expected_size_bytes) || 0))
  const multiplier =
    level.value === 'RAID0'
      ? rows.length
      : level.value === 'RAID1'
        ? 1
        : level.value === 'RAID5'
          ? rows.length - 1
          : rows.length / 2
  return minimum * multiplier
})

const canSubmit = computed(
  () =>
    Boolean(name.value.trim()) &&
    countValid.value &&
    (dryRun.value || confirmed.value) &&
    !submitting.value
)

const onSelectionChange = (rows) => {
  selectedRows.value = rows
}

const reset = async () => {
  name.value = ''
  level.value = 'RAID0'
  stripKb.value = null
  init.value = 'fast'
  dryRun.value = true
  confirmed.value = false
  selectedRows.value = []
  error.value = ''
  await nextTick()
  tableRef.value?.clearSelection()
  const wanted = new Set(props.preselected.map(keyOf).filter(Boolean))
  if (wanted.size) {
    for (const row of availableDisks.value) {
      if (wanted.has(keyOf(row))) tableRef.value?.toggleRowSelection(row, true)
    }
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  }
)

const onVisible = (value) => emit('update:modelValue', value)
const close = () => onVisible(false)

const submit = async () => {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  try {
    const pds = selectedRows.value.map((disk) => {
      const item = {
        serial: disk.serial,
        wwn: disk.wwn,
        expected_size_bytes: Number(disk.expected_size_bytes)
      }
      if (disk.media_type) item.media_type = disk.media_type
      return item
    })
    const params = {
      controller_id: props.controllerId,
      name: name.value.trim(),
      raid_level: level.value,
      pds,
      init: init.value,
      dry_run: dryRun.value
    }
    if (Number(stripKb.value) > 0) params.strip_size_kb = Number(stripKb.value)
    const data = await createTask({
      agent_id: props.agentId,
      type: 'create_raid',
      params,
      require_local_confirm: true
    })
    emit('submitted', [data.task.task_id])
    close()
  } catch (exc) {
    const detail = (exc.fields || []).map((item) => `${item.field}: ${item.message}`).join('；')
    error.value = detail ? `${exc.message}: ${detail}` : exc.message || '下发任务失败'
  } finally {
    submitting.value = false
  }
}
</script>
