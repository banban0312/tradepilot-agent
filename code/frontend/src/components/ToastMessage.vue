<script setup lang="ts">
import { useUiStore } from '../stores/ui'

const ui = useUiStore()
</script>

<template>
  <Transition name="toast">
    <div v-if="ui.toast" class="toast-message" role="status" aria-live="polite">
      <span>{{ ui.toast }}</span>
      <button type="button" aria-label="关闭提示" @click="ui.clearToast()">×</button>
    </div>
  </Transition>
</template>
