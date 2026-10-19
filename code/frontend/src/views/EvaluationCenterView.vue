<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import { evaluationsApi, type EvaluationCase, type EvaluationRun } from '../services/evaluations'

const runs = ref<EvaluationRun[]>([])
const selected = ref<EvaluationRun | null>(null)
const mode = ref<'offline' | 'online'>('offline')
const loading = ref(false)
const starting = ref(false)
const error = ref('')
const expandedCase = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | undefined

const isActive = computed(() => selected.value?.status === 'QUEUED' || selected.value?.status === 'RUNNING')
const failedCases = computed(() => (selected.value?.cases ?? []).filter((item) => item.status === 'FAIL' || item.status === 'ERROR'))
const qualityMetrics = computed(() => Object.entries(selected.value?.summary?.metrics ?? {}))
const statusText: Record<EvaluationRun['status'], string> = { QUEUED: '排队中', RUNNING: '运行中', COMPLETED: '已完成', FAILED: '运行失败' }
const modeText = (value: string) => value === 'offline' ? '离线夹具' : '在线模型'

async function load(runId?: string) {
  loading.value = true
  error.value = ''
  try {
    runs.value = await evaluationsApi.list()
    const targetId = runId ?? selected.value?.run_id ?? runs.value[0]?.run_id
    selected.value = targetId ? await evaluationsApi.get(targetId) : null
    if (selected.value && isActive.value) startPolling()
    else stopPolling()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '读取评测运行失败。'
  } finally {
    loading.value = false
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(() => { void refreshSelected() }, 1500)
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = undefined
}

async function refreshSelected() {
  const runId = selected.value?.run_id
  if (!runId) return
  try {
    selected.value = await evaluationsApi.get(runId)
    runs.value = [selected.value, ...runs.value.filter((run) => run.run_id !== runId)]
    if (!isActive.value) stopPolling()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '刷新评测状态失败。'
    stopPolling()
  }
}

async function startRun() {
  starting.value = true
  error.value = ''
  try {
    selected.value = await evaluationsApi.start(mode.value)
    runs.value = [selected.value, ...runs.value]
    startPolling()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '启动评测失败。'
  } finally {
    starting.value = false
  }
}

function showTrace(item: EvaluationCase) {
  expandedCase.value = expandedCase.value === item.case_id ? null : item.case_id
}

function rate(value: number | null | undefined) {
  return value == null ? '—' : (value * 100).toFixed(1) + '%'
}

function pretty(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2)
}

onMounted(() => { void load() })
onUnmounted(stopPolling)
</script>

<template>
  <main class="eval-page">
    <header class="eval-heading">
      <div><p class="eyebrow">自动评测 · 固定数据集</p><h1>评测中心</h1><p>运行隔离数据域评测，查看分类指标、失败断言和可追溯报告。</p></div>
      <div class="eval-actions">
        <label>运行模式<select v-model="mode" :disabled="starting || isActive"><option value="offline">离线夹具</option><option value="online">在线模型</option></select></label>
        <button class="primary-button" type="button" :disabled="starting || isActive" @click="startRun"><AppIcon name="sparkles" :size="16" />{{ starting ? '正在启动…' : '运行评测' }}</button>
      </div>
    </header>

    <p v-if="error" class="eval-error" role="alert">{{ error }} <button class="text-button" type="button" @click="load(selected?.run_id)">重试</button></p>
    <div v-if="loading && !runs.length" class="eval-state" role="status">正在读取评测记录…</div>
    <div v-else-if="!selected && !error" class="eval-state eval-empty"><strong>暂无评测运行</strong><span>启动一次评测后，这里会显示真实运行报告。</span></div>

    <template v-if="selected">
      <section class="run-card" aria-label="当前评测运行">
        <div class="run-card__identity"><span class="state-pill" :data-state="selected.status">{{ statusText[selected.status] }}</span><span class="mode-pill" :data-mode="selected.mode">{{ modeText(selected.mode) }}</span><code>{{ selected.run_id }}</code></div>
        <div class="run-card__meta"><span>数据集 {{ selected.dataset_version }}</span><span>开始 {{ selected.started_at ? new Date(selected.started_at).toLocaleString('zh-CN') : '等待启动' }}</span><span v-if="selected.completed_at">结束 {{ new Date(selected.completed_at).toLocaleString('zh-CN') }}</span></div>
        <div v-if="selected.summary?.execution_metadata" class="run-card__meta"><span>供应商：{{ selected.summary.execution_metadata.provider ?? '未记录' }}</span><span>模型：{{ selected.summary.execution_metadata.model ?? '未记录' }}</span><span v-if="selected.summary.execution_metadata.model_configuration">模型配置：{{ selected.summary.execution_metadata.model_configuration }}</span><span v-if="selected.summary.code_version">代码版本：{{ selected.summary.code_version }}</span></div>
        <div v-if="isActive" class="progress-block" role="status"><div class="progress-label"><span>{{ selected.status === 'QUEUED' ? '等待评测进程' : '已完成 ' + selected.progress.completed + '/' + selected.progress.total + ' 个案例' }}</span><span>{{ selected.progress.total ? Math.round(selected.progress.completed / selected.progress.total * 100) : 0 }}%</span></div><progress :value="selected.progress.completed" :max="Math.max(selected.progress.total, 1)"></progress><small v-if="selected.progress.case_id">当前案例：{{ selected.progress.case_id }} · {{ selected.progress.case_status }}</small><p>运行过程中不展示最终分数。</p></div>
        <div v-if="selected.status === 'FAILED'" class="eval-error">评测运行未完成（{{ selected.error_code ?? 'EVAL_RUN_FAILED' }}）。可重新运行；系统不会将其计为通过。</div>
        <nav v-if="selected.status === 'COMPLETED'" class="report-links" aria-label="下载报告"><a :href="evaluationsApi.reportUrl(selected.run_id, 'report.md')">下载 Markdown 报告</a><a :href="evaluationsApi.reportUrl(selected.run_id, 'summary.json')">下载 JSON 汇总</a><a :href="evaluationsApi.reportUrl(selected.run_id, 'cases.jsonl')">下载案例明细</a></nav>
      </section>

      <template v-if="selected.status === 'COMPLETED' && selected.summary">
        <section class="metric-grid" aria-label="运行指标">
          <article><span>案例总数</span><strong>{{ selected.summary.case_count }}</strong></article>
          <article><span>通过</span><strong class="success-text">{{ selected.summary.status_counts.PASS ?? 0 }}</strong></article>
          <article><span>失败</span><strong class="failure-text">{{ selected.summary.status_counts.FAIL ?? 0 }}</strong></article>
          <article><span>跳过 / 错误</span><strong>{{ (selected.summary.status_counts.SKIPPED ?? 0) + (selected.summary.status_counts.ERROR ?? 0) }}</strong></article>
          <article v-for="([name, value]) in qualityMetrics" :key="name" class="quality-metric"><span>质量指标 · {{ name }}</span><strong>{{ pretty(value) }}</strong></article>
        </section>
        <section class="category-panel"><header><h2>分类结果</h2><span>{{ modeText(selected.mode) }}结果单独统计</span></header><div class="table-scroll"><table><thead><tr><th>分类</th><th>案例</th><th>通过</th><th>失败</th><th>跳过</th><th>错误</th><th>通过率</th></tr></thead><tbody><tr v-for="(stats, category) in selected.summary.category_counts" :key="category"><th>{{ category }}</th><td>{{ stats.applicable }}</td><td>{{ stats.PASS }}</td><td>{{ stats.FAIL }}</td><td>{{ stats.SKIPPED }}</td><td>{{ stats.ERROR }}</td><td>{{ rate(stats.pass_rate) }}</td></tr></tbody></table></div></section>
        <section class="failure-panel"><header><h2>失败详情</h2><span>{{ failedCases.length }} 条失败或错误</span></header><div v-if="!failedCases.length" class="eval-state eval-state--compact">本次运行没有失败或错误案例。</div><article v-for="item in failedCases" :key="item.case_id" class="failure-card"><header><div><span class="failure-status" :data-status="item.status">{{ item.status === 'FAIL' ? '断言失败' : '执行错误' }}</span><strong>{{ item.case_id }}</strong><small>{{ item.category }} · {{ item.error_code ?? '无错误码' }}{{ item.trace_id ? ' · 轨迹 ' + item.trace_id : '' }}</small></div><button class="text-button" type="button" @click="showTrace(item)">{{ expandedCase === item.case_id ? '收起任务轨迹' : '查看任务轨迹与执行记录' }}</button></header><ul v-if="item.failed_assertions.length"><li v-for="(assertion, index) in item.failed_assertions" :key="index"><strong>{{ assertion.source }}</strong><span>预期：{{ pretty(assertion.expected) }}</span><span>实际：{{ pretty(assertion.actual) }}</span></li></ul><div v-if="expandedCase === item.case_id" class="case-evidence"><div><strong>预期</strong><pre>{{ pretty(item.expected) }}</pre></div><div><strong>实际观测（含隔离任务轨迹）</strong><pre>{{ pretty(item.actual) }}</pre></div></div></article></section>
      </template>
    </template>

    <section v-if="runs.length > 1" class="run-history"><h2>历史运行</h2><button v-for="run in runs" :key="run.run_id" type="button" :aria-current="selected?.run_id === run.run_id" @click="load(run.run_id)"><span>{{ statusText[run.status] }} · {{ modeText(run.mode) }}</span><small>{{ run.started_at ? new Date(run.started_at).toLocaleString('zh-CN') : run.run_id }}</small></button></section>
  </main>
</template>

<style scoped>
.eval-page{max-width:1160px;margin:0 auto;padding:32px clamp(18px,4vw,48px) 64px;color:#1c2a3a}.eval-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:24px}.eval-heading h1{margin:4px 0 8px;font-size:30px}.eval-heading p{margin:0;color:#64748b}.eyebrow{font-size:12px;letter-spacing:.08em;color:#4779a8!important}.eval-actions{display:flex;gap:12px;align-items:flex-end}.eval-actions label{display:grid;gap:7px;font-size:12px;color:#526277}.eval-actions select{height:40px;padding:0 10px;border:1px solid #d8e1eb;border-radius:9px;background:white}.primary-button{display:inline-flex;align-items:center;gap:8px;min-height:40px;padding:0 16px;border:1px solid #2468a8;border-radius:9px;background:#2468a8;color:#fff;cursor:pointer}.primary-button:disabled{opacity:.55;cursor:wait}.eval-state,.eval-error{text-align:center;padding:38px;color:#64748b}.eval-empty{display:grid;gap:8px;background:#fff;border:1px dashed #cbd5e1;border-radius:14px}.eval-error{color:#a33c32;background:#fff5f3;border-radius:10px;padding:14px}.text-button{border:0;background:transparent;color:#2468a8;cursor:pointer}.run-card,.category-panel,.failure-panel,.run-history{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:20px;margin-bottom:16px}.run-card__identity,.run-card__meta,.report-links{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.run-card__identity code{color:#64748b;font-size:12px;overflow-wrap:anywhere}.run-card__meta{margin-top:12px;color:#64748b;font-size:13px}.state-pill,.mode-pill,.failure-status{padding:4px 9px;border-radius:99px;font-size:12px;background:#edf2f7}.state-pill[data-state="COMPLETED"]{background:#e6f7ed;color:#187443}.state-pill[data-state="FAILED"]{background:#fff0ee;color:#a84439}.state-pill[data-state="RUNNING"]{background:#eaf3ff;color:#2468a8}.mode-pill[data-mode="offline"]{background:#f0f3f6;color:#526277}.mode-pill[data-mode="online"]{background:#fff4dd;color:#8a6814}.progress-block{margin-top:18px}.progress-label{display:flex;justify-content:space-between;font-size:13px}.progress-block progress{width:100%;height:10px;margin:8px 0;accent-color:#2468a8}.progress-block p,.progress-block small{font-size:12px;color:#64748b;margin:4px 0}.report-links{margin-top:16px}.report-links a{color:#2468a8;text-decoration:none;font-size:13px}.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.metric-grid article{display:grid;gap:10px;padding:18px;background:#fff;border:1px solid #e2e8f0;border-radius:12px}.metric-grid span{font-size:13px;color:#64748b}.metric-grid strong{font-size:26px}.success-text{color:#187443}.failure-text{color:#a84439}.category-panel header,.failure-panel>header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.category-panel h2,.failure-panel h2,.run-history h2{font-size:18px;margin:0}.category-panel header span,.failure-panel>header span{font-size:12px;color:#64748b}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #edf1f5;white-space:nowrap}thead{background:#f8fafc;color:#64748b}.failure-card{border:1px solid #e8edf2;border-radius:11px;padding:14px;margin-top:10px}.failure-card>header{display:flex;justify-content:space-between;align-items:center;gap:12px}.failure-card>header div{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.failure-card small{color:#64748b}.failure-status[data-status="FAIL"]{color:#a84439;background:#fff0ee}.failure-status[data-status="ERROR"]{color:#805d08;background:#fff4dd}.failure-card ul{padding-left:20px;color:#475569;font-size:13px}.failure-card li{margin:10px 0}.failure-card li span{display:block;margin-top:4px;overflow-wrap:anywhere}.case-evidence{display:grid;grid-template-columns:1fr 1fr;gap:12px}.case-evidence pre{max-height:280px;overflow:auto;padding:12px;background:#f8fafc;border-radius:8px;font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere}.run-history{display:grid;gap:8px}.run-history button{display:flex;justify-content:space-between;gap:12px;text-align:left;padding:12px;border:1px solid #edf1f5;border-radius:8px;background:white;cursor:pointer}.run-history button[aria-current="true"]{border-color:#5b91bd;background:#f4f9fd}.run-history small{color:#64748b}@media(max-width:760px){.eval-heading{align-items:flex-start;flex-direction:column}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.case-evidence{grid-template-columns:1fr}}@media(max-width:520px){.eval-actions{width:100%;justify-content:space-between}.failure-card>header{align-items:flex-start;flex-direction:column}}
.quality-metric strong{font-size:11px;font-weight:500;white-space:pre-wrap;overflow-wrap:anywhere;max-height:140px;overflow:auto}
</style>
