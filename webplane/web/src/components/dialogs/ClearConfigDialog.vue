<template>
  <el-dialog
    :model-value="modelValue"
    title="清除控制器配置"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="onVisible"
  >
    <el-alert
      type="error"
      :closable="false"
      show-icon
      title="高危操作"
      description="将删除该控制器上的所有虚拟盘并清理 foreign/残留元数据，不会重置缓存/BBU 设置。"
    />

    <el-alert
      v-if="!vdsCollected"
      type="warning"
      :closable="false"
      show-icon
      title="尚未采集 RAID 列表"
      description="请先刷新 RAID，确认当前 VD 数量后再提交 clear_config。"
      style="margin-top: 12px"
    />

    <el-form label-position="top" style="margin-top: 12px">
      <el-form-item label="预期 VD 数量">
        <el-input :model-value="String(vdCount)" disabled />
      </el-form-item>
      <el-form-item label="包含 Foreign 配置">
        <el-switch v-model="includeForeign" />
        <span class="hint">agent 只能在明确允许时清理外部配置</span>
      </el-form-item>
      <el-form-item label="仅预演（dry_run）">
        <el-switch v-model="dryRun" />
      </el-form-item>
      <el-checkbox v-if="!dryRun" v-model="confirmed">
        我已确认清除该控制器的全部 RAID 配置
      </el-checkbox>
    </el-form>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="danger" :loading="submitting" :disabled="!canSubmit" @click="submit">
        下发清除任务
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
  vdCount: { type: Number, default: 0 },
  vdsCollected: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue', 'submitted'])

const includeForeign = ref(false)
const dryRun = ref(true)
const confirmed = ref(false)
const submitting = ref(false)
const error = ref('')

const canSubmit = computed(
  () =>
    props.vdsCollected &&
    (dryRun.value || confirmed.value) &&
    !submitting.value
)

const reset = () => {
  includeForeign.value = false
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
      type: 'clear_config',
      params: {
        controller_id: props.controllerId,
        expected_vd_count: Number(props.vdCount),
        include_foreign: includeForeign.value,
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
