<script setup lang="ts">
withDefaults(
  defineProps<{
    title?: string
    message: string
    tone?: 'info' | 'success' | 'warning' | 'danger'
  }>(),
  { tone: 'info' },
)
</script>

<template>
  <div class="notice-banner" :class="`notice-banner--${tone}`" role="status">
    <span class="notice-banner__icon" aria-hidden="true">{{ tone === 'danger' ? '!' : 'i' }}</span>
    <div>
      <strong v-if="title">{{ title }}</strong>
      <p>{{ message }}</p>
    </div>
  </div>
</template>
