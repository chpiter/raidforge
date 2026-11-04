<template>
  <div class="rf-page">
    <PageHeader title="新建任务" subtitle="选择 agent、任务类型并填写参数">
      <template #actions>
        <el-button @click="$router.push('/tasks')">返回任务列表</el-button>
      </template>
    </PageHeader>

    <div class="rf-panel">
      <el-steps :active="step" finish-status="success" simple style="margin-bottom: 20px">
        <el-step title="选择 Agent" />
        <el-step title="任务类型与参数" />
        <el-step title="确认下发" />
      </el-steps>

      <div v-if="step === 0" class="step-body">
        <el-form label-position="top">
          <el-form-item label="目标 Agent" required>
            <el-select v-model="agentId" filterable placeholder="选择在线 agent" style="width: 100%">
              <el-option
                v-for="agent in agents"
                :key="agent.agent_id"
                :label="`${agent.agent_id} · ${agent.server_sn || 'no-sn'} · ${agent.status}`"
                :value="agent.agent_id"
              />
            </el-select>
          </el-form-item>
        </el-form>
        <el-descriptions v-if="selectedAgent" :column="2" border>
          <el-descriptions-item label="Agent">{{ selectedAgent.agent_id }}</el-descriptions-item>
          <el-descriptions-item label="服务器 SN">{{ selectedAgent.server_sn || '-' }}</el-descriptions-item>
          <el-descriptions-item label="状态"><StatusTag :status="selectedAgent.status" /></el-descriptions-item>
          <el-descriptions-item label="型号">
            {{ [selectedAgent.vendor, selectedAgent.model].filter(Boolean).join(' ') || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="控制器" :span="2">
            <span v-if="controllers.length">
              {{ controllers.map((item) => item.controller_id).join(', ') }}
            </span>
            <span v-else class="rf-muted">尚未上报控制器</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div v-else-if="step === 1" class="step-body split">
        <div>
          <el-form label-position="top">
            <el-form-item label="任务类型" required>
              <el-select v-model="typeName" placeholder="选择任务类型" style="width: 100%">
                <el-option-group label="只读">
                  <el-option
                    v-for="item in readTypes"
                    :key="item.type"
                    :label="item.label"
                    :value="item.type"
                  />
                </el-option-group>
                <el-option-group label="写操作">
                  <el-option
                    v-for="item in writeTypes"
                    :key="item.type"
                    :label="item.label"
                    :value="item.type"
                  />
                </el-option-group>
              </el-select>
            </el-form-item>
          </el-form>
          <p v-if="selectedMeta" class="rf-muted type-desc">{{ selectedMeta.description }}</p>
          <TaskTypeForm
            v-if="selectedMeta"
            :meta="selectedMeta"
            :controllers="controllers"
            v-model="params"
          />
        </div>
        <div>
          <h3 class="rf-section-title">参数预览</h3>
          <JsonPreview :value="params" />
        </div>
      </div>

      <div v-else class="step-body split">
        <div>
          <el-form label-position="top">
            <el-form-item label="要求本地确认">
              <el-switch v-model="requireLocalConfirm" />
              <span class="rf-muted hint">agent 对破坏性任务仍会强制本地确认</span>
            </el-form-item>
            <el-form-item label="任务超时（秒）">
              <el-input-number v-model="timeoutSeconds" :min="30" :max="7200" controls-position="right" />
            </el-form-item>
            <el-form-item label="任务有效期（秒）">
              <el-input-number v-model="expiresInSeconds" :min="60" :max="86400" controls-position="right" />
            </el-form-item>
          </el-form>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="Agent">{{ agentId || '-' }}</el-descriptions-item>
            <el-descriptions-item label="任务类型">{{ selectedMeta?.label || typeName }}</el-descriptions-item>
            <el-descriptions-item label="破坏性">
              <el-tag :type="selectedMeta?.destructive ? 'danger' : 'success'" size="small">
                {{ selectedMeta?.destructive ? '是' : '否' }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </div>
        <div>
          <h3 class="rf-section-title">最终参数</h3>
          <JsonPreview :value="params" />
        </div>
      </div>

      <div class="rf-toolbar" style="margin-top: 20px; justify-content: flex-end">
        <el-button v-if="step > 0" @click="step--">上一步</el-button>
        <el-button v-if="step < 2" type="primary" :disabled="!canNext" @click="step++">下一步</el-button>
        <el-button v-else type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
          下发任务
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import JsonPreview from '../components/JsonPreview.vue'
import TaskTypeForm from '../components/TaskTypeForm.vue'
import { createTask, getAgents, getTaskTypes } from '../api'

const route = useRoute()
const router = useRouter()
const agents = ref([])
const taskTypes = ref([])
const step = ref(0)
const agentId = ref(String(route.query.agent_id || ''))
const typeName = ref('')
const params = ref({})
const requireLocalConfirm = ref(true)
const timeoutSeconds = ref(600)
const expiresInSeconds = ref(3600)
const submitting = ref(false)

const selectedAgent = computed(() => agents.value.find((item) => item.agent_id === agentId.value) || null)
const selectedMeta = computed(() => taskTypes.value.find((item) => item.type === typeName.value) || null)
const controllers = computed(() => selectedAgent.value?.capabilities?.controllers || [])
const readTypes = computed(() => taskTypes.value.filter((item) => !item.destructive))
const writeTypes = computed(() => taskTypes.value.filter((item) => item.destructive))

const canNext = computed(() => (step.value === 0 ? Boolean(agentId.value) : Boolean(typeName.value)))
const canSubmit = computed(() => Boolean(agentId.value && typeName.value && selectedMeta.value))

onMounted(async () => {
  const [agentData, typeData] = await Promise.all([
    getAgents({ page: 1, page_size: 200 }),
    getTaskTypes()
  ])
  agents.value = agentData.items || []
  taskTypes.value = typeData.items || []
  if (agentId.value) step.value = 1
})

watch(typeName, () => {
  params.value = {}
})

const submit = async () => {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const data = await createTask({
      agent_id: agentId.value,
      type: typeName.value,
      params: params.value,
      require_local_confirm: requireLocalConfirm.value,
      timeout_seconds: timeoutSeconds.value,
      expires_in_seconds: expiresInSeconds.value
    })
    ElMessage.success('任务已创建')
    router.push(`/tasks/${data.task.task_id}`)
  } catch (exc) {
    const detail = (exc.fields || []).map((item) => `${item.field}: ${item.message}`).join('；')
    ElMessage.error(detail ? `${exc.message}: ${detail}` : exc.message || '创建失败')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.step-body {
  min-height: 240px;
}

.split {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 20px;
}

.type-desc {
  margin: 0 0 12px;
  font-size: 13px;
}

.hint {
  margin-left: 10px;
  font-size: 12px;
}

@media (max-width: 1100px) {
  .split {
    grid-template-columns: 1fr;
  }
}
</style>
