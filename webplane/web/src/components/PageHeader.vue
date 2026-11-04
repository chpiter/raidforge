<template>
  <div class="page-header">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="subtitle" class="rf-muted">{{ subtitle }}</p>
    </div>
    <div class="actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' }
})
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

h2 {
  margin: 0;
  font-size: 20px;
}

p {
  margin: 4px 0 0;
  font-size: 13px;
}

.actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
