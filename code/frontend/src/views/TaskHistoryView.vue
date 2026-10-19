<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { tasksApi, type TaskBatchTree, type TaskHistoryFilters, type TaskSummary } from '../services/tasks'

const router = useRouter()
const filters = reactive<TaskHistoryFilters>({ state: '', input_type: '', order_type: '', q: '' })
const tasks = ref<TaskSummary[]>([])
const batches = ref<TaskBatchTree[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const error = ref('')
const loaded = ref(false)
let loadGeneration = 0

function queryFilters(): TaskHistoryFilters {
  return {
    ...filters,
    created_after: filters.created_after ? new Date(filters.created_after).toISOString() : undefined,
    created_before: filters.created_before ? new Date(filters.created_before).toISOString() : undefined,
  }
}

async function load(reset = false) {
  if (loading.value && !reset) return
  const generation = reset ? ++loadGeneration : loadGeneration
  loading.value = true
  error.value = ''
  try {
    const result = await tasksApi.list(queryFilters(), reset ? null : nextCursor.value, 20)
    if (generation !== loadGeneration) return
    tasks.value = reset ? result.items : [...tasks.value, ...result.items]
    const knownBatches = new Map((reset ? [] : batches.value).map((batch) => [batch.batch_id, batch]))
    for (const batch of result.batches ?? []) knownBatches.set(batch.batch_id, batch)
    batches.value = [...knownBatches.values()]
    nextCursor.value = result.next_cursor
    loaded.value = true
  } catch (caught) {
    if (generation !== loadGeneration) return
    error.value = caught instanceof Error ? caught.message : '读取历史任务失败，请稍后重试。'
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

function resetFilters() {
  void load(true)
}

function continueTask(task: TaskSummary) {
  // 继续操作复用原任务 ID，由工作台根据服务端状态判断可用操作。
  void router.push({ name: 'task-workbench', params: { taskId: task.task_id } })
}

function stateLabel(state: string) {
  const labels: Record<string, string> = {
    QUEUED: '排队中', RUNNING: '处理中', WAITING_INPUT: '等待补充',
    WAITING_APPROVAL: '待审批', SUBMITTING: '提交中', BLOCKED_RULE: '规则阻断',
    FAILED: '失败', COMPLETED: '已完成', REJECTED: '已退回',
  }
  return labels[state] ?? state
}

function batchStateLabel(state: string) {
  const labels: Record<string, string> = { QUEUED: '排队中', STOPPING: '正在停止', STOPPED: '已停止', COMPLETED: '已完成', PARTIAL: '部分完成', FAILED: '失败' }
  return labels[state] ?? '处理中'
}

function groupStateLabel(state: string) {
  const labels: Record<string, string> = { READY_FOR_REVIEW: '待审核', OVER_QUOTA_REVIEW: '超额待审核', NEEDS_CORRECTION: '需要修正' }
  return labels[state] ?? '未知状态'
}

onMounted(() => { void load(true) })
</script>

<template>
  <main class="history-page">
    <header class="history-heading">
      <div><p class="eyebrow">任务记录</p><h1>历史任务</h1><p>按状态和输入条件筛选，并从原任务继续或查看真实执行轨迹。</p></div>
      <button class="primary-button" type="button" @click="router.push({ name: 'workbench' })"><AppIcon name="sparkles" :size="17" />开始新任务</button>
    </header>

    <form class="history-filters" @submit.prevent="resetFilters">
      <label class="history-search">关键词<input v-model="filters.q" maxlength="120" placeholder="任务编号、描述或会话标题" /></label>
      <label>任务状态<select v-model="filters.state"><option value="">全部状态</option><option value="QUEUED">排队中</option><option value="RUNNING">处理中</option><option value="WAITING_INPUT">等待补充</option><option value="WAITING_APPROVAL">待审批</option><option value="SUBMITTING">提交中</option><option value="BLOCKED_RULE">规则阻断</option><option value="FAILED">失败</option><option value="COMPLETED">已完成</option><option value="REJECTED">已退回</option></select></label>
      <label>订单类型<select v-model="filters.order_type"><option value="">全部类型</option><option value="SALES">销售</option><option value="PURCHASE">采购</option></select></label>
      <label>输入来源<select v-model="filters.input_type"><option value="">全部来源</option><option value="TEXT">文字</option><option value="TEXT_AND_FILES">文字与附件</option><option value="EXCEL_GROUP">表格分组</option></select></label>
      <label>开始时间<input v-model="filters.created_after" type="datetime-local" /></label>
      <label>结束时间<input v-model="filters.created_before" type="datetime-local" /></label>
      <div class="history-filter-actions"><button class="primary-button" type="submit" :disabled="loading">查询</button><button class="text-button" type="button" @click="Object.assign(filters, { state: '', input_type: '', order_type: '', q: '', created_after: '', created_before: '' }); resetFilters()">清除</button></div>
    </form>

    <p v-if="error" class="history-error" role="alert">{{ error }} <button class="text-button" type="button" @click="load(true)">重试</button></p>
    <div v-if="loading && !loaded" class="history-state" role="status">正在读取历史任务…</div>
    <div v-else-if="loaded && !tasks.length && !error" class="history-state"><strong>没有找到符合条件的任务</strong><span>调整筛选条件后再试。</span></div>
    <section v-else class="history-list" aria-label="历史任务列表">
      <section v-if="batches.length" class="batch-tree" aria-label="批次父子任务"><h2>批次任务树</h2><article v-for="batch in batches" :key="batch.batch_id" class="batch-card"><header><strong>导入批次</strong><code>{{ batch.batch_id }}</code><span>{{ batchStateLabel(batch.state) }}</span><small>{{ new Date(batch.created_at).toLocaleString('zh-CN') }}</small></header><div class="batch-groups"><div v-for="group in batch.groups" :key="group.group_id" class="batch-group"><span class="tree-branch">└─</span><div><strong>{{ group.sheet_name }} · 分组 {{ group.group_id }}</strong><small>源行：{{ group.source_rows.join('、') }} · {{ groupStateLabel(group.preview_status) }}</small></div><span>{{ group.task_state ? stateLabel(group.task_state) : group.task_id ? '任务不可见' : '尚未创建子任务' }}</span><button v-if="group.task_id" class="text-button" type="button" @click="router.push({ name: 'task-trace', params: { taskId: group.task_id } })">查看子任务</button></div></div></article></section>
      <article v-for="task in tasks" :key="task.task_id" class="history-card">
        <div class="history-card__main">
          <div class="history-card__title"><span class="history-state-badge" :data-state="task.state">{{ stateLabel(task.state) }}</span><strong>{{ task.order_type === 'SALES' ? '销售任务' : '采购任务' }}</strong></div>
          <code>{{ task.task_id }}</code>
          <p>{{ task.input_type === 'TEXT_AND_FILES' ? '文字与附件' : task.input_type === 'EXCEL_GROUP' ? '表格分组' : '文字输入' }} · 草稿版本 {{ task.draft_version ?? '暂无' }}</p>
          <small>创建于 {{ new Date(task.created_at).toLocaleString('zh-CN') }} · 更新于 {{ new Date(task.updated_at).toLocaleString('zh-CN') }}</small>
        </div>
        <div class="history-card__actions">
          <button class="text-button" type="button" @click="router.push({ name: 'task-trace', params: { taskId: task.task_id } })">查看轨迹</button>
          <button class="primary-button" type="button" @click="continueTask(task)">打开原任务</button>
        </div>
      </article>
    </section>
    <div v-if="nextCursor" class="history-more"><button class="secondary-button" type="button" :disabled="loading" @click="load()">{{ loading ? '正在读取…' : '加载更多' }}</button></div>
  </main>
</template>

<style scoped>
.history-page{max-width:1120px;margin:0 auto;padding:32px clamp(18px,4vw,48px) 64px;color:#1c2a3a}.history-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:24px}.history-heading h1{margin:4px 0 8px;font-size:30px}.history-heading p{margin:0;color:#64748b}.eyebrow{font-size:12px;letter-spacing:.08em;color:#4779a8!important}.history-filters{display:grid;grid-template-columns:2fr repeat(3,minmax(120px,1fr));gap:14px;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:20px;margin-bottom:20px}.history-filters label{display:grid;gap:7px;font-size:12px;color:#526277}.history-filters input,.history-filters select{min-width:0;height:40px;border:1px solid #d8e1eb;border-radius:9px;padding:0 10px;background:#fff;color:#1c2a3a}.history-search{grid-column:span 2}.history-filter-actions{display:flex;align-items:end;gap:12px}.history-card{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:20px;background:#fff;border:1px solid #e2e8f0;border-radius:14px;margin-top:10px}.history-card__main{display:grid;gap:8px;min-width:0}.history-card__title{display:flex;align-items:center;gap:10px}.history-card code{font-size:12px;color:#64748b;overflow-wrap:anywhere}.history-card p,.history-card small{margin:0;color:#64748b}.history-card__actions{display:flex;align-items:center;gap:16px;flex-shrink:0}.history-state-badge{font-size:12px;padding:4px 9px;background:#edf2f7;border-radius:99px}.history-state-badge[data-state="COMPLETED"]{background:#e6f7ed;color:#187443}.history-state-badge[data-state="FAILED"]{background:#fff0ee;color:#a84439}.history-state-badge[data-state^="WAITING"]{background:#fff6df;color:#8a6814}.history-state,.history-error{text-align:center;padding:42px;color:#64748b}.history-state{display:grid;gap:8px}.history-error{color:#b42318}.history-more{text-align:center;padding:24px}.primary-button,.secondary-button{min-height:38px;padding:0 14px;border-radius:9px;border:1px solid #2468a8;background:#2468a8;color:white;cursor:pointer}.secondary-button{background:white;color:#2468a8}.text-button{border:0;background:transparent;color:#2468a8;cursor:pointer}.primary-button:disabled,.secondary-button:disabled{opacity:.55;cursor:wait}@media(max-width:800px){.history-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.history-search{grid-column:span 2}.history-heading{align-items:flex-start;flex-direction:column}}@media(max-width:600px){.history-card{align-items:flex-start;flex-direction:column}.history-card__actions{width:100%;justify-content:flex-end}.history-filters{grid-template-columns:1fr}.history-search{grid-column:auto}}
.batch-tree{margin-bottom:20px}.batch-tree h2{font-size:18px}.batch-card{padding:16px;background:#f8fafc;border:1px solid #dce6ef;border-radius:14px;margin:10px 0}.batch-card header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.batch-card header code{color:#64748b;font-size:12px}.batch-card header small{margin-left:auto;color:#64748b}.batch-groups{margin:12px 0 0 10px;padding-left:14px;border-left:1px solid #b8c9d9}.batch-group{display:flex;align-items:center;gap:12px;min-height:48px;border-top:1px solid #e8edf2}.batch-group>div{display:grid;gap:3px;flex:1}.batch-group small,.batch-group>span{color:#64748b;font-size:12px}.tree-branch{color:#7891a8}
</style>
