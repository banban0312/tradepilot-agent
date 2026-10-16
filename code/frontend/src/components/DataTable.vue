<script setup lang="ts">
export type TableColumn = { key: string; label: string; align?: 'left' | 'right' }

defineProps<{
  columns: TableColumn[]
  rows: Array<Record<string, string | number>>
  emptyMessage?: string
}>()
</script>

<template>
  <div class="data-table-wrap">
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key" :class="`text-${column.align ?? 'left'}`">
            {{ column.label }}
          </th>
        </tr>
      </thead>
      <tbody v-if="rows.length">
        <tr v-for="(row, index) in rows" :key="String(row.id ?? index)">
          <td v-for="column in columns" :key="column.key" :class="`text-${column.align ?? 'left'}`">
            <slot :name="`cell-${column.key}`" :row="row" :value="row[column.key]">
              {{ row[column.key] ?? '—' }}
            </slot>
          </td>
        </tr>
      </tbody>
      <tbody v-else>
        <tr><td :colspan="columns.length" class="data-table__empty">{{ emptyMessage ?? '暂无数据' }}</td></tr>
      </tbody>
    </table>
  </div>
</template>
