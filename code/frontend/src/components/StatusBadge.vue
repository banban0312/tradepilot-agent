<script setup lang="ts">
withDefaults(
  defineProps<{
    label: string
    tone?: 'neutral' | 'info' | 'success' | 'warning' | 'danger'
  }>(),
  { tone: 'neutral' },
)
</script>

<template>
  <span class="status-badge" :class="`status-badge--${tone}`">{{ label }}</span>
</template>
