<template>
  <div v-if="meta" class="task-form">
    <el-alert
      v-if="meta.destructive"
      type="error"
      :closable="false"
      show-icon
      title="破坏性任务"
      description="该任务会修改阵列卡或磁盘。请确认目标 serial/wwn/size 与现场一致；dry_run 默认开启。"
      class="alert"
    />
    <el-form label-position="top">
      <el-form-item
        v-for="field in meta.fields"
        :key="field.name"
        :label="field.label"
        :required="field.required"
      >
        <el-input
          v-if="field.type === 'text'"
          v-model="form[field.name]"
          :placeholder="field.placeholder || field.label"
          clearable
        />

        <el-input-number
          v-else-if="field.type === 'number'"
          v-model="form[field.name]"
          :min="field.min"
          :max="field.max"
          :value-on-clear="null"
          controls-position="right"
          style="width: 100%"
        />

        <el-switch
          v-else-if="field.type === 'boolean'"
          v-model="form[field.name]"
          :disabled="field.name === 'verify' && meta.type === 'erase_disks'"
        />

        <el-select
          v-else-if="field.type === 'select'"
          v-model="form[field.name]"
          style="width: 100%"
        >
          <el-option
            v-for="option in field.options || []"
            :key="typeof option === 'string' ? option : option.value"
            :label="typeof option === 'string' ? option : option.label || option.value"
            :value="typeof option === 'string' ? option : option.value"
          />
        </el-select>

        <el-select
          v-else-if="field.type === 'controller'"
          v-model="form[field.name]"
          filterable
          clearable
          style="width: 100%"
          placeholder="选择控制器"
        >
          <el-option
            v-for="controller in controllers"
            :key="controller.controller_id"
            :label="controllerLabel(controller)"
            :value="controller.controller_id"
          />
        </el-select>

        <el-select
          v-else-if="field.type === 'string-list'"
          v-model="form[field.name]"
          multiple
          filterable
          allow-create
          default-first-option
          clearable
          style="width: 100%"
          placeholder="留空表示全部"
        >
          <el-option
            v-for="controller in controllers"
            :key="controller.controller_id"
            :label="controller.controller_id"
            :value="controller.controller_id"
          />
        </el-select>

        <div v-else-if="field.type === 'selectors'" class="selectors">
          <el-table :data="form[field.name]" size="small" border>
            <el-table-column label="Serial" min-width="150">
              <template #default="{ row }">
                <el-input v-model="row.serial" placeholder="serial" />
              </template>
            </el-table-column>
            <el-table-column label="WWN (0x…)" min-width="180">
              <template #default="{ row }">
                <el-input v-model="row.wwn" placeholder="0x5000c500a4a2b001" />
              </template>
            </el-table-column>
            <el-table-column label="容量 (bytes)" width="150">
              <template #default="{ row }">
                <el-input-number v-model="row.expected_size_bytes" :min="1" controls-position="right" style="width: 100%" />
              </template>
            </el-table-column>
            <el-table-column label="介质" width="110">
              <template #default="{ row }">
                <el-select v-model="row.media_type" clearable placeholder="可选">
                  <el-option label="HDD" value="hdd" />
                  <el-option label="SSD" value="ssd" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="" width="76" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" @click="removeSelector($index)">移除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-button size="small" style="margin-top: 8px" @click="addSelector">添加磁盘</el-button>
        </div>

        <div v-if="field.help" class="field-help">{{ field.help }}</div>
      </el-form-item>
    </el-form>

    <el-alert
      v-if="requiresControllers && !controllers.length"
      type="warning"
      :closable="false"
      show-icon
      title="该 agent 尚未上报控制器信息"
      description="请先下发 refresh_hardware 或 list_controllers，再创建需要控制器的任务。"
    />
  </div>
</template>

<script setup>
import { computed, reactive, watch } from 'vue'

const props = defineProps({
  meta: { type: Object, default: null },
  controllers: { type: Array, default: () => [] },
  modelValue: { type: Object, default: () => ({}) }
})

const emit = defineEmits(['update:modelValue'])
const form = reactive({})

const requiresControllers = computed(() =>
  (props.meta?.fields || []).some((field) => ['controller', 'string-list', 'selectors'].includes(field.type))
)

function ensureShape() {
  for (const field of props.meta?.fields || []) {
    if (field.type === 'string-list' && !Array.isArray(form[field.name])) {
      form[field.name] = []
    }
    if (field.type === 'selectors' && !Array.isArray(form[field.name])) {
      form[field.name] = []
    }
  }
}

function resetForm() {
  for (const key of Object.keys(form)) delete form[key]
  if (props.meta) {
    const template = JSON.parse(JSON.stringify(props.meta.template || {}))
    Object.assign(form, template)
    ensureShape()
  }
}

function addSelector() {
  const field = (props.meta?.fields || []).find((item) => item.type === 'selectors')
  if (!field) return
  if (!Array.isArray(form[field.name])) form[field.name] = []
  form[field.name].push({ serial: '', wwn: '', expected_size_bytes: 1, media_type: '' })
}

function removeSelector(index) {
  const field = (props.meta?.fields || []).find((item) => item.type === 'selectors')
  if (field && Array.isArray(form[field.name])) form[field.name].splice(index, 1)
}

function clean() {
  if (!props.meta) return {}
  const raw = JSON.parse(JSON.stringify(form))
  const type = props.meta.type

  if (Array.isArray(raw.controller_ids)) {
    raw.controller_ids = raw.controller_ids.length ? raw.controller_ids : null
  }
  if (typeof raw.include_raw_output !== 'undefined') {
    raw.include_raw_output = Boolean(raw.include_raw_output)
  }
  if (type === 'abort_current') {
    raw.reason = raw.reason || 'operator requested'
    raw.target_task_id = raw.target_task_id || ''
  }
  if (Array.isArray(raw.pds)) {
    raw.pds = raw.pds
      .map((row) => ({
        serial: String(row.serial || '').trim(),
        wwn: String(row.wwn || '').trim(),
        expected_size_bytes: Number(row.expected_size_bytes) || 0,
        media_type: row.media_type || ''
      }))
      .filter((row) => row.serial || row.wwn || row.expected_size_bytes)
      .map((row) => {
        const item = {
          serial: row.serial,
          wwn: row.wwn,
          expected_size_bytes: row.expected_size_bytes
        }
        if (row.media_type) item.media_type = row.media_type
        return item
      })
  }
  if (typeof raw.expected_size_bytes !== 'undefined') {
    raw.expected_size_bytes = Number(raw.expected_size_bytes) || 0
  }
  if (typeof raw.expected_vd_count !== 'undefined') {
    raw.expected_vd_count = Number(raw.expected_vd_count) || 0
  }
  if (typeof raw.strip_size_kb !== 'undefined') {
    const stripe = Number(raw.strip_size_kb)
    if (!raw.strip_size_kb || !Number.isFinite(stripe) || stripe <= 0) {
      delete raw.strip_size_kb
    } else {
      raw.strip_size_kb = stripe
    }
  }
  if (typeof raw.passes !== 'undefined') {
    raw.passes = Number(raw.passes) || 1
  }
  for (const key of Object.keys(raw)) {
    if (raw[key] === '' && !['name', 'vd_id', 'vd_name', 'reason', 'target_task_id', 'controller_id'].includes(key)) {
      delete raw[key]
    }
  }
  if (type === 'erase_disks') raw.verify = false
  return raw
}

function controllerLabel(controller) {
  const parts = [controller.controller_id]
  if (controller.vendor) parts.push(controller.vendor)
  if (controller.model) parts.push(controller.model)
  return parts.join(' · ')
}

watch(() => props.meta, resetForm, { immediate: true })
watch(
  form,
  () => emit('update:modelValue', clean()),
  { deep: true, immediate: true }
)
</script>

<style scoped>
.task-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.alert {
  margin-bottom: 4px;
}

.field-help {
  width: 100%;
  color: var(--rf-muted);
  font-size: 12px;
  line-height: 1.4;
  margin-top: 4px;
}

.selectors {
  width: 100%;
}
</style>
