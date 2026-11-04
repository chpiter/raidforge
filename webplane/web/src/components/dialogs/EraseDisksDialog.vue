<template>
  <el-dialog
    :model-value="modelValue"
    title="擦除磁盘"
    width="760px"
    :close-on-click-modal="false"
    @update:model-value="onVisible"
  >
    <el-alert
      type="error"
      :closable="false"
      show-icon
      title="破坏性操作"
      description="擦除会在阵列卡固件中异步执行，返回成功只代表已提交。请核对 serial / wwn / size。"
    />

    <el-table :data="disks" size="small" border style="margin-top: 12px">
      <el-table-column prop="serial" label="Serial" min-width="140" />
      <el-table-column prop="wwn" label="WWN" min-width="180" class-name="rf-mono" />
      <el-table-column label="容量" width="110">
        <template #default="{ row }">{{ formatBytes(row.expected_size_bytes) }}</template>
      </el-table-column>
      <el-table-column prop="raid_member_of" label="RAID 成员" width="110">
        <template #default="{ row }">{{ row.raid_member_of || '-' }}</template>
      </el-table-column>
    </el-table>

    <el-form label-position="top" style="margin-top: 12px">
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="擦除模式">
            <el-select v-model="mode" style="width: 100%">
              <el-option label="thorough" value="thorough" />
              <el-option label="overwrite" value="overwrite" />
              <el-option label="erase" value="erase" />
              <el-option label="blockerase" value="blockerase" />
              <el-option label="sanitize" value="sanitize" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="遍数">
            <el-input-number v-model="passes" :min="1" :max="3" controls-position="right" style="width: 100%" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="允许 RAID 成员盘">
        <el-switch v-model="allowRaidMember" :disabled="!hasRaidMember" />
        <span class="hint">
          {{ hasRaidMember ? '选中的磁盘属于 RAID；需显式允许，agent 仍会二次校验。' : '未选中 RAID 成员盘。' }}
        </span>
      </el-form-item>
      <el-form-item label="仅预演（dry_run）">
        <el-switch v-model="dryRun" />
      </el-form-item>
      <el-checkbox v-if="!dryRun" v-model="confirmed">
        我已确认目标 serial / wwn / size 与现场一致
      </el-checkbox>
    </el-form>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="danger" :loading="submitting" :disabled="!canSubmit" @click="submit">
        下发擦除任务
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { createTask } from '../../api'
import { formatBytes } from '../../composables/usePolling'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agentId: { type: String, required: true },
  controllerId: { type: String, required: true },
  disks: { type: Array, default: () => [] }
})
const emit = defineEmits(['update:modelValue', 'submitted'])
const mode = ref('thorough')
const passes = ref(1)
const allowRaidMember = ref(false)
const dryRun = ref(true)
const confirmed = ref(false)
const submitting = ref(false)
const error = ref('')

const hasRaidMember = computed(() => props.disks.some((disk) => disk.raid_member_of))
const canSubmit = computed(
  () => props.disks.length > 0 && (dryRun.value || confirmed.value) && !submitting.value
)

const reset = () => {
  mode.value = 'thorough'
  passes.value = 1
  allowRaidMember.value = false
  dryRun.value = true
  confirmed.value = false
  error.value = ''
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  }
)

const onVisible = (value) => {
  emit('update:modelValue', value)
}

const close = () => onVisible(false)

const submit = async () => {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  try {
    const pds = props.disks.map((disk) => {
      const item = {
        serial: disk.serial,
        wwn: disk.wwn,
        expected_size_bytes: Number(disk.expected_size_bytes)
      }
      if (disk.media_type) item.media_type = disk.media_type
      return item
    })
    const data = await createTask({
      agent_id: props.agentId,
      type: 'erase_disks',
      params: {
        controller_id: props.controllerId,
        mode: mode.value,
        passes: passes.value,
        verify: false,
        allow_raid_member: allowRaidMember.value,
        pds,
        dry_run: dryRun.value
      },
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

<style scoped>
.hint {
  margin-left: 10px;
  color: var(--rf-muted);
  font-size: 12px;
}
</style>
