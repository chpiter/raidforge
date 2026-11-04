<template>
  <el-tag :type="tagType" effect="light" size="small">{{ label }}</el-tag>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: String, default: 'unknown' }
})

const LABELS = {
  online: '在线',
  offline: '离线',
  unknown: '未知',
  pending: '待派发',
  leased: '已派发',
  running: '运行中',
  finished: '已完成',
  failed: '失败',
  cancelled: '已取消',
  expired: '已过期'
}

const TYPES = {
  online: 'success',
  offline: 'danger',
  unknown: 'info',
  pending: 'warning',
  leased: 'primary',
  running: 'primary',
  finished: 'success',
  failed: 'danger',
  cancelled: 'info',
  expired: 'info'
}

const label = computed(() => LABELS[props.status] || props.status)
const tagType = computed(() => TYPES[props.status] || 'info')
</script>
