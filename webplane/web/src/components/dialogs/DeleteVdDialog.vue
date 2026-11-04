<template>
  <el-dialog
    :model-value="modelValue"
    title="删除 RAID 卷"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="onVisible"
  >
    <el-alert
      type="error"
      :closable="false"
      show-icon
      title="破坏性操作"
      description="删除虚拟盘会移除其上的数据。agent 会按 vd_id + 名称 + 容量三重校验。"
    />

    <el-form label-position="top" style="margin-top: 12px">
      <el-form-item label="VD ID">
        <el-input v-model="vdId" disabled />
      </el-form-item>
      <el-form-item label="VD 名称" required>
        <el-input v-model="vdName" placeholder="缓存为空时手工填写" />
      </el-form-item>
      <el-form-item label="预期容量 (bytes)" required>
        <el-input-number v-model="expectedSize" :min="1" controls-position="right" style="width: 100%" />
      </el-form-item>
      <el-form-item label="仅预演（dry_run）">
        <el-switch v-model="dryRun" />
      </el-form-item>
      <el-checkbox v-if="!dryRun" v-model="confirmed">
        我已确认该 RAID 卷及其成员盘
      </el-checkbox>
    </el-form>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="danger" :loading="submitting" :disabled="!canSubmit" @click="submit">
        下发删除任务
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { createTask } from '../../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agentId: { type: String, required: true },
  controllerId: { type: String, required: true },
  vd: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue', 'submitted'])

const vdId = ref('')
const vdName = ref('')
const expectedSize = ref(1)
const dryRun = ref(true)
const confirmed = ref(false)
const submitting = ref(false)
const error = ref('')

const canSubmit = computed(
  () =>
    Boolean(vdId.value && vdName.value.trim() && expectedSize.value > 0) &&
    (dryRun.value || confirmed.value) &&
    !submitting.value
)

const reset = () => {
  vdId.value = props.vd?.vd_id || ''
  vdName.value = props.vd?.vd_name || ''
  expectedSize.value = Number(props.vd?.size_bytes) || 1
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

const onVisible = (value) => emit('update:modelValue', value)
const close = () => onVisible(false)

const submit = async () => {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  try {
    const data = await createTask({
      agent_id: props.agentId,
      type: 'delete_raid',
      params: {
        controller_id: props.controllerId,
        vd_id: vdId.value,
        vd_name: vdName.value.trim(),
        expected_size_bytes: Number(expectedSize.value),
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
