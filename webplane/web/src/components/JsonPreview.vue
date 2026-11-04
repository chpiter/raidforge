<template>
  <pre class="rf-json">{{ text }}</pre>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  value: { type: [Object, Array, String, Number, Boolean, null], default: null }
})

const text = computed(() => {
  if (typeof props.value === 'string') return props.value
  return JSON.stringify(props.value ?? {}, null, 2)
})
</script>
