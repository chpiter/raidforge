<template>
  <div class="stat" :class="tone">
    <div class="label">{{ label }}</div>
    <div class="value">{{ value }}</div>
    <div v-if="hint" class="hint">{{ hint }}</div>
  </div>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '-' },
  hint: { type: String, default: '' },
  tone: { type: String, default: 'default' }
})
</script>

<style scoped>
.stat {
  background: #fff;
  border: 1px solid var(--rf-border);
  border-radius: 10px;
  padding: 18px;
  border-top: 3px solid #cbd5e1;
}

.stat.success {
  border-top-color: #16a34a;
}

.stat.warning {
  border-top-color: #d97706;
}

.stat.danger {
  border-top-color: #dc2626;
}

.stat.primary {
  border-top-color: #2563eb;
}

.label {
  color: var(--rf-muted);
  font-size: 13px;
}

.value {
  font-size: 30px;
  font-weight: 700;
  margin-top: 6px;
}

.hint {
  color: var(--rf-muted);
  font-size: 12px;
  margin-top: 4px;
}
</style>
