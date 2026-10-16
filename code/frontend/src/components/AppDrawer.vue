<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'

const props = defineProps<{ modelValue: boolean; title: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

watch(
  () => props.modelValue,
  (open) => {
    document.body.classList.toggle('drawer-open', open)
    if (open) document.addEventListener('keydown', onKeydown)
    else document.removeEventListener('keydown', onKeydown)
  },
  { immediate: true },
)

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && props.modelValue) close()
}

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  document.body.classList.remove('drawer-open')
})

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="drawer-backdrop" @click.self="close" @keydown.esc="close">
      <section class="app-drawer" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1">
        <header class="app-drawer__header">
          <h2>{{ title }}</h2>
          <button class="icon-button" type="button" aria-label="关闭面板" @click="close">×</button>
        </header>
        <div class="app-drawer__body"><slot /></div>
      </section>
    </div>
  </Teleport>
</template>
