<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { tasksApi, type TaskTrace, type TaskTraceSpan } from '../services/tasks'

const route = useRoute()
const router = useRouter()
const trace = ref<TaskTrace | null>(null)
const spans = ref<TaskTraceSpan[]>([])
const cursor = ref<string | null>(null)
const loading = ref(false)
const error = ref('')
const taskId = computed(() => String(route.params.taskId ?? ''))
let loadGeneration = 0

function stateLabel(value: string) {
  const map: Record<string, string> = { QUEUED: '排队中', RUNNING: '处理中', WAITING_INPUT: '等待补充', WAITING_APPROVAL: '待审批', COMPLETED: '已完成', FAILED: '失败', REJECTED: '已退回', OK: '成功', ERROR: '失败', INTERRUPTED: '中断', APPROVE: '批准', RETURN: '退回修改', REJECT: '拒绝' }
  return map[value] ?? value
}

function formatJson(value: Record<string, unknown> | null) {
  return value ? JSON.stringify(value, null, 2) : '暂无可展示的安全摘要'
}

async function load(reset = false) {
  if ((loading.value && !reset) || !taskId.value) return
  const generation = reset ? ++loadGeneration : loadGeneration
  loading.value = true
  error.value = ''
  try {
    const result = await tasksApi.getTrace(taskId.value, reset ? null : cursor.value)
    if (generation !== loadGeneration) return
    trace.value = result
    spans.value = reset ? result.spans : [...spans.value, ...result.spans]
    cursor.value = result.next_cursor
  } catch (caught) {
    if (generation !== loadGeneration) return
    error.value = caught instanceof Error ? caught.message : '读取执行轨迹失败。'
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

watch(taskId, () => { trace.value = null; spans.value = []; cursor.value = null; void load(true) })
onMounted(() => { void load(true) })
</script>

<template>
  <main class="trace-page">
    <header class="trace-heading">
      <div><button class="text-button" type="button" @click="router.push({ name: 'task-history' })">返回历史任务</button><p class="eyebrow">执行记录</p><h1>任务执行轨迹</h1><code>{{ taskId }}</code></div>
      <button class="secondary-button" type="button" @click="router.push({ name: 'task-workbench', params: { taskId } })">打开原任务</button>
    </header>

    <p v-if="error" class="trace-error" role="alert">{{ error }} <button class="text-button" type="button" @click="load(true)">重试</button></p>
    <div v-if="loading && !trace" class="trace-empty" role="status">正在读取任务轨迹…</div>
    <template v-else-if="trace">
      <section class="trace-metrics" aria-label="任务统计">
        <article><span>任务状态</span><strong>{{ stateLabel(trace.state) }}</strong></article>
        <article><span>模型调用</span><strong>{{ trace.metrics.model_calls }}</strong></article>
        <article><span>Token 用量</span><strong>{{ trace.metrics.tokens?.total_tokens ?? '不可用' }}</strong><small>覆盖 {{ trace.metrics.usage_coverage === null ? '不可用' : `${Math.round(trace.metrics.usage_coverage * 100)}%` }}（{{ trace.metrics.usage_available_calls }}/{{ trace.metrics.model_calls }} 次）</small></article>
        <article><span>模型耗时</span><strong>{{ trace.metrics.model_duration_ms }} 毫秒</strong></article>
        <article><span>工具耗时</span><strong>{{ trace.metrics.tool_duration_ms }} 毫秒</strong></article>
      </section>

      <section class="trace-section">
        <div class="section-heading"><div><h2>执行节点</h2><p>按真实调用时间排列；输入与输出仅展示已脱敏结构摘要。</p></div></div>
        <div v-if="!spans.length" class="trace-empty">此任务尚无可查询的执行节点。</div>
        <article v-for="span in spans" :key="span.span_id" class="span-card">
          <div class="span-title"><span class="kind-chip">{{ span.kind }}</span><strong>{{ span.name }}</strong><span class="status-chip" :data-status="span.status">{{ stateLabel(span.status) }}</span><span class="span-duration">{{ span.duration_ms === null ? '耗时不可用' : `${span.duration_ms} 毫秒` }}</span></div>
          <div class="span-meta"><span>尝试 {{ span.attempt }}</span><span>{{ span.protocol ?? span.execution_mode }}</span><span>{{ new Date(span.start_at).toLocaleString('zh-CN') }}</span><span v-if="span.usage">输入 {{ span.usage.input_tokens }} · 输出 {{ span.usage.output_tokens }} · 总计 {{ span.usage.total_tokens }} Token</span><span v-else>Token 用量不可用</span></div>
          <p v-if="span.error.code" class="span-error">错误位置：{{ span.name }} · {{ span.error.code }}{{ span.error.retryable ? '（可重试）' : '' }}</p>
          <div class="span-details"><div><h3>调用摘要</h3><pre>{{ formatJson(span.input) }}</pre></div><div><h3>结果摘要</h3><pre>{{ formatJson(span.output) }}</pre></div></div>
          <small v-if="span.parent_span_id" class="parent-ref">父节点：{{ span.parent_span_id }}</small>
        </article>
        <div v-if="cursor" class="trace-more"><button class="secondary-button" type="button" :disabled="loading" @click="load()">{{ loading ? '正在读取…' : '加载更多节点' }}</button></div>
      </section>

      <section class="trace-section wait-section"><div class="section-heading"><div><h2>人工等待与检查点</h2><p>人工等待单独统计，不计入模型或工具耗时；检查点来自任务恢复记录。</p></div></div><p v-if="!trace.metrics.manual_waits.length" class="muted">没有人工等待记录。</p><div v-for="(wait, index) in trace.metrics.manual_waits" :key="`${wait.start_at}-${index}`" class="wait-row"><strong>{{ stateLabel(wait.state ?? '') }}</strong><span>{{ new Date(wait.start_at).toLocaleString('zh-CN') }}{{ wait.end_at ? ` 至 ${new Date(wait.end_at).toLocaleString('zh-CN')}` : ' · 当前等待中' }}</span><span>{{ wait.duration_ms }} 毫秒</span></div><p v-if="!trace.checkpoints.length" class="muted">没有检查点记录。</p><div v-for="checkpoint in trace.checkpoints" :key="checkpoint.checkpoint_id + checkpoint.interrupt_id" class="relation-row"><strong>{{ checkpoint.kind === 'APPROVAL' ? '审批检查点' : '补充信息检查点' }}</strong><span>草稿版本 {{ checkpoint.draft_version ?? '暂无' }} · {{ checkpoint.active ? '当前有效' : '已结束' }}</span><code>{{ checkpoint.checkpoint_id }}</code></div></section>

      <section class="trace-relations"><article class="trace-section"><h2>审批记录</h2><p v-if="!trace.approvals.length" class="muted">没有审批记录。</p><div v-for="approval in trace.approvals" :key="approval.approval_id" class="relation-row"><strong>{{ approval.decision }}</strong><span>草稿版本 {{ approval.version }} · {{ approval.invalidated ? '已失效' : '有效' }}</span><small>{{ new Date(approval.created_at).toLocaleString('zh-CN') }}</small></div></article><article class="trace-section"><h2>订单关联</h2><p v-if="!trace.order" class="muted">尚未生成正式订单。</p><div v-else class="relation-row"><strong>{{ trace.order.order_number }}</strong><span>{{ stateLabel(trace.order.state) }}</span></div></article></section>

      <details class="event-log"><summary>状态与事件记录（{{ trace.events.length }} 条）</summary><p v-if="trace.events_truncated || trace.audit_events_truncated" class="muted">记录数量达到当前展示上限；完整事件可通过任务事件回查接口继续读取。</p><ol><li v-for="event in trace.events" :key="event.seq"><time>{{ new Date(event.emitted_at).toLocaleString('zh-CN') }}</time><strong>{{ event.type }}</strong><span>{{ event.payload.state ? stateLabel(String(event.payload.state)) : event.payload.code ?? event.payload.name ?? '' }}</span></li></ol><h3>操作审计</h3><p v-if="!trace.audit_events.length" class="muted">没有操作审计记录。</p><ol><li v-for="(event, index) in trace.audit_events" :key="`${event.action}-${event.created_at}-${index}`"><time>{{ new Date(event.created_at).toLocaleString('zh-CN') }}</time><strong>{{ event.action }}</strong><span>{{ event.result }} · 操作人 {{ event.actor_id ?? '系统' }}</span></li></ol></details>
    </template>
  </main>
</template>

<style scoped>
.trace-page{max-width:1180px;margin:0 auto;padding:32px clamp(18px,4vw,48px) 64px;color:#1c2a3a}.trace-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:24px}.trace-heading h1{font-size: var(--font-size-page);margin:6px 0}.trace-heading code{font-size: var(--font-size-sm);color:#64748b;overflow-wrap:anywhere}.eyebrow{font-size: var(--font-size-sm);letter-spacing:.08em;color:#4779a8;margin:16px 0 0}.text-button{border:0;background:transparent;color:#2468a8;cursor:pointer;padding:0}.secondary-button{min-height:38px;padding:0 14px;border-radius:9px;border:1px solid #2468a8;background:white;color:#2468a8;cursor:pointer}.trace-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:20px}.trace-metrics article,.trace-section,.span-card,.event-log{background:#fff;border:1px solid #e2e8f0;border-radius:14px}.trace-metrics article{padding:16px;display:grid;gap:8px}.trace-metrics span,.trace-metrics small,.muted{color:#64748b}.trace-metrics strong{font-size: var(--font-size-xl)}.trace-section{padding:20px;margin:16px 0}.section-heading{display:flex;justify-content:space-between;margin-bottom:14px}.section-heading h2,.trace-section h2{margin:0 0 6px}.section-heading p{margin:0;color:#64748b}.span-card{padding:18px;margin:10px 0}.span-title{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.span-title strong{overflow-wrap:anywhere}.kind-chip,.status-chip{font-size: var(--font-size-sm);padding:4px 8px;border-radius:99px;background:#edf4fa;color:#375a78}.status-chip[data-status="ERROR"],.status-chip[data-status="INTERRUPTED"]{background:#fff0ee;color:#a84439}.status-chip[data-status="OK"]{background:#e6f7ed;color:#187443}.span-duration{margin-left:auto;color:#526277;font-size: var(--font-size-base)}.span-meta{display:flex;gap:8px 18px;flex-wrap:wrap;margin-top:12px;color:#64748b;font-size: var(--font-size-sm)}.span-error{color:#a84439}.span-details{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}.span-details h3{font-size: var(--font-size-sm);color:#526277}.span-details pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;padding:12px;border-radius:9px;background:#f5f8fb;color:#334155;font-size: var(--font-size-sm)}.parent-ref{color:#64748b}.trace-more{text-align:center;padding:14px}.wait-row,.relation-row{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:12px 0;border-top:1px solid #edf0f4}.wait-row span,.relation-row span,.relation-row small{color:#64748b}.trace-relations{display:grid;grid-template-columns:1fr 1fr;gap:16px}.event-log{padding:18px}.event-log summary{cursor:pointer;font-weight:600}.event-log ol{padding-left:22px}.event-log li{display:grid;grid-template-columns:minmax(150px,auto) 1fr 1fr;gap:12px;padding:8px 0;border-top:1px solid #edf0f4}.event-log time,.event-log span{color:#64748b}.trace-empty,.trace-error{text-align:center;padding:40px;color:#64748b}.trace-error{color:#a84439}@media(max-width:850px){.trace-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.trace-relations{grid-template-columns:1fr}}@media(max-width:600px){.trace-heading{align-items:flex-start;flex-direction:column}.span-details{grid-template-columns:1fr}.wait-row,.relation-row{align-items:flex-start;flex-direction:column}.event-log li{grid-template-columns:1fr}}
</style>
