<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <template v-if="name === 'sparkles'"><path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3Z"/><path d="m19 15 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z"/></template>
    <template v-else-if="name === 'file'"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h8"/></template>
    <template v-else-if="name === 'grid'"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9m6 12V9"/></template>
    <template v-else-if="name === 'trace'"><rect x="4" y="3" width="6" height="6" rx="1"/><rect x="14" y="15" width="6" height="6" rx="1"/><path d="M7 9v3a3 3 0 0 0 3 3h4"/></template>
    <template v-else-if="name === 'chart'"><path d="M3 3v18h18"/><path d="m7 14 4-4 4 3 6-7"/></template>
    <template v-else-if="name === 'sliders'"><path d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M2 14h4m4-6h4m4 8h4"/></template>
    <template v-else-if="name === 'shield'"><path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/></template>
    <template v-else-if="name === 'info'"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/></template>
    <template v-else-if="name === 'history'"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5m4-1v5l3 2"/></template>
    <template v-else-if="name === 'upload'"><path d="M12 16V4m-5 5 5-5 5 5"/><path d="M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></template>
    <template v-else-if="name === 'plus'"><path d="M12 5v14m-7-7h14"/></template>
    <template v-else-if="name === 'refresh'"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.5-2L20 12M4 12l2.9 5a7 7 0 0 0 11.5-2"/></template>
    <template v-else-if="name === 'check'"><path d="m5 12 4 4L19 6"/></template>
    <template v-else-if="name === 'save'"><path d="M5 3h12l4 4v14H3V3Z"/><path d="M7 3v6h10V3M7 21v-8h10v8"/></template>
    <template v-else-if="name === 'arrow'"><path d="M5 12h14m-6-6 6 6-6 6"/></template>
    <template v-else-if="name === 'close'"><path d="m18 6-12 12M6 6l12 12"/></template>
    <template v-else-if="name === 'user'"><circle cx="12" cy="8" r="4"/><path d="M5 21a7 7 0 0 1 14 0"/></template>
    <template v-else><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></template>
  </svg>
</template>
