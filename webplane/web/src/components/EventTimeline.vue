<template>
  <el-timeline v-if="items.length">
    <el-timeline-item
      v-for="item in items"
      :key="item.id"
      :timestamp="formatTime(item.created_at)"
      placement="top"
      :type="typeFor(item)"
    >
      <div class="event-head">
        <el-tag size="small" effect="plain">{{ item.kind }}</el-tag>
        <span v-if="item.phase" class="rf-muted">{{ item.phase }}</span>
        <span class="rf-muted">· {{ item.actor }}</span>
      </div>
      <div v-if="item.message" class="event-msg">{{ item.message }}</div>
      <pre v-if="hasData(item.data)" class="event-data rf-mono">{{ JSON.stringify(item.data) }}</pre>
    </el-timeline-item>
  </el-timeline>
  <el-empty v-else description="暂无事件" />
</template>

<script setup>
import { formatTime } from '../composables/usePolling'

defineProps({
  items: { type: Array, default: () => [] }
})

const hasData = (value) => value && typeof value === 'object' && Object.keys(value).length > 0

const typeFor = (item) => {
  if (item.kind === 'result') return item.phase === 'failed' ? 'danger' : 'success'
  if (item.kind === 'expired' || item.kind === 'cancel_requested') return 'warning'
  if (item.kind === 'started' || item.kind === 'progress') return 'primary'
  return ''
}
</script>

<style scoped>
.event-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.event-msg {
  margin-top: 4px;
  font-size: 13px;
}

.event-data {
  margin: 6px 0 0;
  padding: 6px 8px;
  background: #f1f5f9;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
