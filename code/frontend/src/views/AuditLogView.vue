<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import { adminConfigApi, type AuditRecord } from '../services/adminConfig'

const filters = reactive({ task_id: '', actor_id: '', department_id: '', action: '', created_after: '', created_before: '', input_hash: '', reference: '' })
const rows = ref<AuditRecord[]>([])
const cursor = ref<string | null>(null)
const nextCursor = ref<string | null>(null)
const busy = ref(false)
const error = ref('')
const selected = ref<AuditRecord | null>(null)

async function search(next = false) {
  busy.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ limit: '50' })
    for (const [key, value] of Object.entries(filters)) if (value.trim()) {
      params.set(key, key.startsWith('created_') ? new Date(value).toISOString() : value.trim())
    }
    if (next && cursor.value) params.set('cursor', cursor.value)
    const page = await adminConfigApi.audit(params)
    rows.value = next ? [...rows.value, ...page.items] : page.items
    nextCursor.value = page.next_cursor
    cursor.value = page.next_cursor
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取审计流水失败。' }
  finally { busy.value = false }
}

onMounted(() => search())
</script>

<template>
  <section class="audit-log">
    <header class="page-heading"><div><h1>系统操作审计</h1><p>按操作人、动作、任务、输入摘要和时间范围查询责任流水。附件内容仍按原权限单独读取。</p></div><RouterLink class="secondary-button" :to="{ name: 'runtime-settings' }">运行参数与业务规则</RouterLink></header>
    <NoticeBanner v-if="error" tone="danger" title="查询失败" :message="error" />
    <form class="surface-card audit-log__filters" @submit.prevent="search(false)">
      <label>操作人 ID<input v-model="filters.actor_id" placeholder="UUID" /></label>
      <label>部门 ID<input v-model="filters.department_id" placeholder="UUID" /></label>
      <label>操作动作<input v-model="filters.action" placeholder="如 task.created" /></label>
      <label>任务 / 订单关联任务 ID<input v-model="filters.task_id" placeholder="UUID" /></label>
      <label>订单号 / 对象编号<input v-model="filters.reference" placeholder="订单号、草稿或审批编号" /></label>
      <label>输入 SHA-256<input v-model="filters.input_hash" minlength="64" maxlength="64" placeholder="64 位摘要" /></label>
      <label>开始时间<input v-model="filters.created_after" type="datetime-local" /></label>
      <label>结束时间<input v-model="filters.created_before" type="datetime-local" /></label>
      <button class="primary-button" type="submit" :disabled="busy">{{ busy ? '查询中…' : '查询审计' }}</button>
    </form>
    <div class="surface-card audit-log__table-wrap">
      <table class="audit-log__table"><thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象 / 版本</th><th>结果</th><th>摘要</th><th>详情</th></tr></thead>
        <tbody><tr v-for="row in rows" :key="row.id"><td>{{ new Date(row.created_at).toLocaleString('zh-CN') }}</td><td>{{ row.actor }}<small class="audit-log__department">{{ row.department }}</small></td><td>{{ row.action }}</td><td><RouterLink v-if="row.task_id" :to="{ name: 'task-trace', params: { taskId: row.task_id } }">{{ row.object_ref?.order_number ?? row.task_id.slice(0, 8) + '…' }}</RouterLink><span v-else>{{ row.object_ref?.config_snapshot_id ?? row.object_ref?.artifact_id ?? '—' }}</span></td><td>{{ row.result }}</td><td><code>{{ row.input_hash?.slice(0, 12) ?? '—' }}</code></td><td><button class="audit-log__link" type="button" @click="selected = row">查看</button></td></tr>
          <tr v-if="!busy && !rows.length"><td colspan="7" class="audit-log__empty">没有符合条件的审计记录。</td></tr></tbody>
      </table>
      <div v-if="nextCursor" class="audit-log__more"><button class="secondary-button" type="button" :disabled="busy" @click="search(true)">加载更多</button></div>
    </div>
    <section v-if="selected" class="surface-card audit-log__details"><header><h2>操作详情</h2><button class="secondary-button" @click="selected = null">关闭</button></header><p>记录 ID：{{ selected.id }}</p><p>关联任务：{{ selected.task_id ?? '无' }}</p><p>输入摘要：<code>{{ selected.input_hash ?? '无' }}</code></p><pre>{{ JSON.stringify({ 对象: selected.object_ref, 修改前: selected.before_ref, 修改后: selected.after_ref }, null, 2) }}</pre></section>
  </section>
</template>

<style scoped>
.audit-log { display: grid; gap: 16px; }
.audit-log__filters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: end; gap: 12px; padding: 18px; }
.audit-log__filters label { display: grid; gap: 6px; color: var(--color-muted); font-size: var(--font-size-sm); }
.audit-log__filters input { height: 39px; min-width: 0; border: 1px solid var(--color-border); border-radius: 5px; padding: 0 9px; }
.audit-log__table-wrap { overflow: auto; padding: 14px; }
.audit-log__table { width: 100%; border-collapse: collapse; font-size: var(--font-size-sm); white-space: nowrap; }
.audit-log__table th { background: #f3f7f9; color: var(--color-muted); text-align: left; }
.audit-log__table th, .audit-log__table td { border-bottom: 1px solid var(--color-border); padding: 12px 10px; }
.audit-log__table a, .audit-log__link { color: var(--color-primary); }
.audit-log__department { display: block; margin-top: 3px; color: var(--color-muted); }
.audit-log__link { border: 0; background: transparent; cursor: pointer; }
.audit-log__empty { padding: 30px !important; color: var(--color-muted); text-align: center; }
.audit-log__more { display: flex; justify-content: center; padding-top: 12px; }
.audit-log__details { padding: 18px; }
.audit-log__details header { display: flex; justify-content: space-between; align-items: center; }
.audit-log__details h2 { margin: 0; font-size: var(--font-size-md); }
.audit-log__details pre { overflow: auto; padding: 12px; border-radius: 6px; background: #f3f7f9; font-size: var(--font-size-sm); }
@media (max-width: 760px) { .audit-log__filters { grid-template-columns: 1fr 1fr; } }
</style>
