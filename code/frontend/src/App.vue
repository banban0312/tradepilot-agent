<script setup lang="ts">
import { onMounted } from 'vue'
import { useRuntimeStore } from './stores/runtime'

const runtime = useRuntimeStore()
onMounted(() => runtime.refresh())
</script>

<template>
  <RouterView />
</template>
