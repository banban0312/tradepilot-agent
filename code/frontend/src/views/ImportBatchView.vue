<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import NoticeBanner from '../components/NoticeBanner.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { importsApi, type ImportBatch, type ImportBatchGroup } from '../services/imports'
import { tasksApi, type DraftCandidate, type TaskSnapshot } from '../services/tasks'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const auth = useAuthStore()
const batch = ref<ImportBatch | null>(null)
const tasks = ref<Record<string, TaskSnapshot>>({})
const forms = ref<Record<string, DraftCandidate>>({})
const candidateForms = ref<Record<string, DraftCandidate>>({})
const selected = ref(new Set<string>())
const loading = ref(true)
const stopping = ref(false)
const approving = ref(false)
const reviewConfirmOpen = ref(false)
const saving = ref('')
const retrying = ref('')
const checking = ref('')
const retryTarget = ref<ImportBatchGroup | null>(null)
const error = ref('')
const result = ref<Record<string, string>>({})
let timer: ReturnType<typeof setInterval> | undefined
let busy = false

const batchId = computed(() => String(route.params.batchId ?? ''))
const canApprove = computed(() => Boolean(auth.identity?.roles.includes('business_approver')))
const canOperate = computed(() => Boolean(auth.identity?.roles.includes('business_operator')))
const eligibleGroups = computed(() => (batch.value?.groups ?? []).filter(group => eligible(group)))
const selectedGroups = computed(() => eligibleGroups.value.filter(group => selected.value.has(groupKey(group))))
const selectedAmount = computed(() => {
  const cents = eligibleGroups.value.filter(group => selected.value.has(groupKey(group)))
    .reduce((sum, group) => sum + moneyCents(group.task_id ? tasks.value[group.task_id]?.draft?.total_amount : null), 0n)
  return `${cents / 100n}.${(cents % 100n).toString().padStart(2, '0')}`
})

function groupKey(group: ImportBatchGroup) { return `${group.sheet}\u0000${group.group_id}` }

function eligible(group: ImportBatchGroup) {
  const task = group.task_id ? tasks.value[group.task_id] : undefined
  const quota = task?.quota_preview
  return Boolean(canApprove.value && task?.state === 'WAITING_APPROVAL'
    && task.active_wait?.kind === 'APPROVAL' && task.draft?.validated && task.draft.candidate
    && task.available_actions.includes('APPROVE') && quota?.balance_available
    && Number(quota.excess_amount ?? 0) <= 0)
}

function stateLabel(group: ImportBatchGroup) {
  if (!group.task_id) return group.preview_status === 'OVER_QUOTA_REVIEW' ? '额度阻断' : '需补充'
  const task = tasks.value[group.task_id]
  const state = task?.state ?? group.state
  return ({ QUEUED: '排队中', STARTING: '正在启动', RUNNING: '处理中', WAITING_INPUT: '待补充', WAITING_APPROVAL: '待审批', SUBMITTING: '结果核实中', BLOCKED_RULE: '规则阻断', FAILED: '处理失败', COMPLETED: '已生成', REJECTED: '已拒绝', STOPPED: '已停止', NEEDS_CORRECTION: '需修正', OVER_QUOTA_REVIEW: '额度阻断', READY_FOR_REVIEW: '待处理' } as Record<string, string>)[state] ?? state
}

function tone(group: ImportBatchGroup) {
  if (group.preview_status !== 'READY_FOR_REVIEW' || ['FAILED', 'BLOCKED_RULE', 'REJECTED'].includes(group.task_status ?? '')) return 'danger'
  const state = group.task_id ? tasks.value[group.task_id]?.state ?? group.state : group.state
  if (state === 'COMPLETED') return 'success'
  if (state === 'WAITING_INPUT' || state === 'WAITING_APPROVAL') return 'warning'
  return 'info'
}

async function refresh() {
  if (busy || !batchId.value) return
  busy = true
  try {
    const fresh = await importsApi.getBatch(batchId.value)
    batch.value = fresh
    for (const group of fresh.groups) {
      if (!group.task_id && group.candidate && !candidateForms.value[groupKey(group)]) {
        candidateForms.value[groupKey(group)] = structuredClone(group.candidate) as unknown as DraftCandidate
      }
    }
    const detailStates = new Set(['WAITING_INPUT', 'WAITING_APPROVAL', 'SUBMITTING', 'BLOCKED_RULE', 'FAILED', 'REJECTED', 'COMPLETED'])
    const toFetch = fresh.groups.filter(group => group.task_id && detailStates.has(group.task_status ?? '')
      && (tasks.value[group.task_id]?.state !== group.task_status || ['WAITING_APPROVAL', 'SUBMITTING'].includes(group.task_status ?? '')))
    const snapshots: Array<readonly [string, TaskSnapshot | null]> = []
    for (let index = 0; index < toFetch.length; index += 8) {
      const chunk = toFetch.slice(index, index + 8)
      snapshots.push(...await Promise.all(chunk.map(async group => {
        try { return [group.task_id!, await tasksApi.get(group.task_id!)] as const }
        catch { return [group.task_id!, null] as const }
      })))
    }
    for (const [id, task] of snapshots) {
      if (!task) continue
      tasks.value[id] = task
      if (task.draft?.candidate && !forms.value[id]) forms.value[id] = structuredClone(task.draft.candidate)
    }
    for (const group of fresh.groups) if (!eligible(group)) selected.value.delete(groupKey(group))
    error.value = ''
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '无法读取批次。'
  } finally {
    loading.value = false
    busy = false
  }
}

function toggle(group: ImportBatchGroup, event: Event) {
  if (!eligible(group)) return
  const checked = (event.target as HTMLInputElement).checked
  if (checked) selected.value.add(groupKey(group))
  else selected.value.delete(groupKey(group))
}

function selectEligible() {
  selected.value = new Set(eligibleGroups.value.map(groupKey))
}

async function saveDraft(group: ImportBatchGroup) {
  if (!group.task_id) return
  const task = tasks.value[group.task_id]
  const form = forms.value[group.task_id]
  if (!task || !form || !task.available_actions.includes('EDIT_DRAFT')) return
  saving.value = groupKey(group)
  try {
    await tasksApi.patchDraft(task, form)
    result.value[groupKey(group)] = '修改已提交，服务端正在重新校验；新版本需要重新审批。'
    delete tasks.value[group.task_id]
    delete forms.value[group.task_id]
    await refresh()
  } catch (caught) {
    result.value[groupKey(group)] = caught instanceof Error ? caught.message : '修改失败，请刷新后重试。'
  } finally {
    saving.value = ''
  }
}

async function saveCorrection(group: ImportBatchGroup) {
  const form = candidateForms.value[groupKey(group)]
  if (!batch.value || !form || group.task_id || saving.value) return
  saving.value = groupKey(group)
  try {
    batch.value = await importsApi.correctBatchGroup(batch.value.batch_id, group, form)
    result.value[groupKey(group)] = '已保存并重新校验。符合条件的订单组已进入独立任务队列。'
    delete candidateForms.value[groupKey(group)]
    await refresh()
  } catch (caught) {
    result.value[groupKey(group)] = caught instanceof Error ? caught.message : '修正失败，请刷新后重试。'
  } finally {
    saving.value = ''
  }
}

function openApprovalReview() {
  if (selectedGroups.value.length) reviewConfirmOpen.value = true
}

async function approveSelected() {
  const previouslySelected = [...selectedGroups.value]
  if (approving.value || !previouslySelected.length) return
  approving.value = true
  error.value = ''
  try {
    const latestBatch = await importsApi.getBatch(batchId.value)
    batch.value = latestBatch
    const latestRows = latestBatch.groups.filter(group => previouslySelected.some(selectedGroup => groupKey(selectedGroup) === groupKey(group)))
    const latestTasks = await Promise.all(latestRows.filter(group => group.task_id).map(async group => [group.task_id!, await tasksApi.get(group.task_id!)] as const))
    for (const [id, task] of latestTasks) tasks.value[id] = task
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '提交前无法刷新审批版本，请稍后重试。'
    approving.value = false
    return
  }
  const targets = selectedGroups.value
  if (targets.length !== previouslySelected.length) {
    reviewConfirmOpen.value = false
    error.value = '所选草稿版本或审批状态已变化，请重新核对后选择。'
    approving.value = false
    return
  }
  const outcomes: Record<string, string> = {}
  for (const group of targets) {
    const task = group.task_id ? tasks.value[group.task_id] : undefined
    if (!task) continue
    try {
      await tasksApi.review(task, 'APPROVE')
      outcomes[groupKey(group)] = '已逐笔提交审批，等待任务执行。'
    } catch (caught) {
      outcomes[groupKey(group)] = caught instanceof Error ? caught.message : '本笔审批失败。'
    }
  }
  result.value = { ...result.value, ...outcomes }
  selected.value.clear()
  approving.value = false
  reviewConfirmOpen.value = false
  await refresh()
}

async function stopBatch() {
  if (!batch.value || stopping.value || batch.value.stop_requested) return
  stopping.value = true
  try { batch.value = await importsApi.stopBatch(batchId.value) }
  catch (caught) { error.value = caught instanceof Error ? caught.message : '停止批次失败。' }
  finally { stopping.value = false; await refresh() }
}

function failedSpanId(task: TaskSnapshot | undefined) {
  const value = task?.last_error?.failed_span_id
  return typeof value === 'string' ? value : ''
}

function failureName(task: TaskSnapshot | undefined) {
  const failure = task?.last_error?.failure
  return failure && typeof failure === 'object' && 'name' in failure && typeof failure.name === 'string'
    ? failure.name
    : '失败执行步骤'
}

function startRetry(group: ImportBatchGroup) {
  if (!canOperate.value || !group.task_id) return
  const task = tasks.value[group.task_id]
  if (task?.state !== 'FAILED' || !task.available_actions.includes('RETRY') || !failedSpanId(task)) return
  retryTarget.value = group
}

async function confirmRetry() {
  const group = retryTarget.value
  if (!group?.task_id || retrying.value) return
  const taskId = group.task_id
  const key = groupKey(group)
  const expectedSpan = failedSpanId(tasks.value[taskId])
  retrying.value = key
  error.value = ''
  try {
    const latest = await tasksApi.get(taskId)
    tasks.value[taskId] = latest
    if (latest.state !== 'FAILED' || !latest.available_actions.includes('RETRY') || failedSpanId(latest) !== expectedSpan) {
      result.value[key] = '失败位置或任务状态已变化；已刷新服务端状态，没有提交旧版本重试。'
      retryTarget.value = null
      return
    }
    await tasksApi.retry(latest)
    result.value[key] = '安全恢复请求已登记；正在查询原任务的最新执行结果。'
    retryTarget.value = null
    delete tasks.value[taskId]
  } catch (caught) {
    result.value[key] = caught instanceof Error
      ? `${caught.message} 已刷新最新任务状态，请先核实结果再决定是否恢复。`
      : '恢复请求未确认；正在回查原任务，不会创建新任务或正式订单。'
    retryTarget.value = null
    delete tasks.value[taskId]
  } finally {
    retrying.value = ''
    await refresh()
  }
}

async function verifyResult(group: ImportBatchGroup) {
  if (!group.task_id || checking.value) return
  const key = groupKey(group)
  checking.value = key
  error.value = ''
  try {
    const fresh = await importsApi.getBatch(batchId.value)
    batch.value = fresh
    const latestGroup = fresh.groups.find(item => groupKey(item) === key)
    if (latestGroup?.task_id) {
      const snapshot = await tasksApi.get(latestGroup.task_id)
      tasks.value[latestGroup.task_id] = snapshot
      result.value[key] = snapshot.order_ref
        ? `已核实正式订单 ${snapshot.order_ref.order_number}，不会再次提交。`
        : snapshot.state === 'COMPLETED'
          ? '任务已完成，正在刷新批次中的订单关联。'
          : `已核实任务状态：${stateLabel(latestGroup)}。当前没有可确认的正式订单。`
    } else {
      result.value[key] = '批次已更新；订单组当前没有关联子任务。'
    }
  } catch (caught) {
    result.value[key] = caught instanceof Error ? caught.message : '无法核实结果，请稍后刷新。'
  } finally {
    checking.value = ''
    await refresh()
  }
}

function formatMoney(value: string | null | undefined) {
  if (!value) return '待核算'
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(Number(value))
}

function moneyCents(value: string | null | undefined) {
  const match = value?.match(/^(\d+)(?:\.(\d{1,2}))?$/)
  if (!match) return 0n
  return BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0'))
}

onMounted(() => { void refresh(); timer = setInterval(() => void refresh(), 6000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <section class="batch-page">
    <header class="page-heading">
      <div><p class="eyebrow">批量导入 / {{ batch?.batch_id.slice(0, 8) }}</p><h1>订单分组与逐笔校验</h1><p class="page-description">每个订单组独立处理。修改后重新校验；批准按草稿版本和内容摘要逐笔提交。</p></div>
      <div class="page-heading__actions"><RouterLink class="secondary-button" :to="{ name: 'workbench' }">返回工作台</RouterLink><button v-if="canOperate" class="secondary-button" type="button" :disabled="stopping || batch?.stop_requested" @click="stopBatch">{{ stopping ? '正在停止…' : batch?.stop_requested ? '已请求停止' : '停止未开始任务' }}</button></div>
    </header>
    <NoticeBanner v-if="error" tone="danger" :message="error" />
    <div v-if="loading && !batch" class="batch-card">正在读取批次和任务状态…</div>
    <template v-else-if="batch">
      <div class="batch-metrics">
        <article><span>订单组</span><strong>{{ batch.summary.group_count }}</strong></article>
        <article><span>处理中</span><strong>{{ batch.summary.running_count }}</strong></article>
        <article><span>已生成</span><strong>{{ batch.summary.completed_count }}</strong></article>
        <article><span>等待补充</span><strong>{{ batch.summary.waiting_input_count }}</strong></article>
        <article><span>待审批</span><strong>{{ batch.summary.waiting_approval_count }}</strong></article>
        <article><span>结果核实中</span><strong>{{ batch.summary.result_check_count }}</strong></article>
        <article><span>规则 / 额度阻断</span><strong>{{ batch.summary.needs_correction_count + batch.summary.over_quota_count + batch.summary.blocked_count }}</strong></article>
        <article><span>处理失败 / 已拒绝</span><strong>{{ batch.summary.failed_count }}</strong></article>
      </div>
      <section class="batch-card">
        <div class="batch-card__heading"><div><h2>订单分组与校验结果</h2><p>来源行号、草稿版本和额度状态自动刷新</p></div><button v-if="canApprove" class="secondary-button" type="button" :disabled="!eligibleGroups.length" @click="selectEligible">选择全部可审批项</button></div>
        <div class="batch-table-wrap"><table class="batch-table"><thead><tr><th>选择 / 订单组</th><th>来源行</th><th>合同 / 类型</th><th>金额 / 额度</th><th>版本 / 摘要</th><th>状态</th><th>异常与操作</th></tr></thead>
          <tbody v-for="group in batch.groups" :key="groupKey(group)" :class="{ 'batch-row--blocked': !group.task_id || group.preview_status !== 'READY_FOR_REVIEW' }">
            <tr>
              <td><label class="batch-select"><input type="checkbox" :checked="selected.has(groupKey(group))" :disabled="!eligible(group)" @change="toggle(group, $event)"><strong>{{ group.group_id }}</strong></label><small>工作表 {{ group.sheet }}</small><small v-if="group.task_id">任务 {{ group.task_id.slice(0, 8) }}</small></td>
              <td>{{ group.source_rows.join('、') }}</td>
              <td><template v-if="group.task_id && tasks[group.task_id]?.draft?.candidate"><code>{{ tasks[group.task_id]?.draft?.candidate?.contract_id?.slice(0, 12) }}…</code><small>{{ tasks[group.task_id]?.draft?.candidate?.order_type === 'SALES' ? '销售' : '采购' }}</small></template><span v-else>—</span></td>
              <td><strong>{{ formatMoney(group.task_id ? tasks[group.task_id]?.draft?.total_amount : null) }}</strong><small v-if="group.task_id && tasks[group.task_id]?.quota_preview">当前可用余额 {{ formatMoney(tasks[group.task_id]?.quota_preview?.remaining_amount) }}</small><small v-if="group.task_id && Number(tasks[group.task_id]?.quota_preview?.excess_amount ?? 0) > 0">额度已变化，当前超出 {{ formatMoney(tasks[group.task_id]?.quota_preview?.excess_amount) }}</small></td>
              <td><template v-if="group.task_id && tasks[group.task_id]?.draft">v{{ tasks[group.task_id]?.draft?.version }}<small>摘要 {{ tasks[group.task_id]?.draft?.content_hash.slice(0, 12) }}…</small></template><span v-else>—</span></td>
              <td><StatusBadge :label="stateLabel(group)" :tone="tone(group)" /></td>
              <td class="batch-row-actions"><RouterLink v-if="group.order_id" class="batch-link" :to="{ name: 'order-center', query: { order_id: group.order_id } }">查看正式订单 {{ group.order_number }}</RouterLink><RouterLink v-else-if="group.task_id" class="batch-link" :to="{ name: 'task-workbench', params: { taskId: group.task_id } }">打开任务</RouterLink><span v-else>未创建子任务</span><button v-if="group.task_id && (group.state === 'SUBMITTING' || tasks[group.task_id]?.state === 'SUBMITTING')" class="text-button" type="button" :disabled="checking === groupKey(group)" @click="verifyResult(group)">{{ checking === groupKey(group) ? '正在核实…' : '核实提交结果' }}</button><RouterLink v-if="group.task_id && failedSpanId(tasks[group.task_id])" class="batch-link" :to="{ name: 'task-workbench', params: { taskId: group.task_id }, query: { failed_span_id: failedSpanId(tasks[group.task_id]) } }">查看失败轨迹 · {{ failureName(tasks[group.task_id]) }}</RouterLink><button v-if="canOperate && group.task_id && tasks[group.task_id]?.available_actions.includes('RETRY')" class="text-button" type="button" :disabled="retrying === groupKey(group)" @click="startRetry(group)">安全恢复失败任务</button></td>
            </tr>
            <tr v-if="group.execution_error" class="batch-details"><td colspan="7"><NoticeBanner tone="danger" :message="`${String(group.execution_error.code ?? 'TASK_FAILED')} · ${String(group.execution_error.message ?? '任务执行失败。')}`" /></td></tr>
            <tr v-if="group.issues.length" class="batch-details"><td colspan="7"><ul><li v-for="(issue, index) in group.issues" :key="`${issue.code}-${index}`"><strong>{{ issue.locator.cell ?? `第 ${issue.locator.row ?? '?'} 行` }}</strong> {{ issue.message }}</li></ul><small v-if="!group.task_id">可在此修正缺失字段并重新校验；合同或项目无法匹配时请修正 Excel 后重新导入。</small></td></tr>
            <tr v-if="group.task_id && tasks[group.task_id]?.draft?.candidate && tasks[group.task_id]?.available_actions.includes('EDIT_DRAFT')" class="batch-details"><td colspan="7"><div class="batch-edit-grid"><label>交期<input v-model="forms[group.task_id].delivery_date" type="date"></label><label>收货地址<input v-model="forms[group.task_id].address" placeholder="填写地址"></label><label>联系人<input v-model="forms[group.task_id].contact" placeholder="填写联系人"></label><label v-for="(item, index) in forms[group.task_id].items" :key="index">{{ item.raw_name || `物料 ${index + 1}` }} · 数量<input v-model="item.quantity" inputmode="decimal"></label><label v-for="(item, index) in forms[group.task_id].items" :key="`price-${index}`">{{ item.raw_name || `物料 ${index + 1}` }} · 单价<input v-model="item.unit_price" inputmode="decimal"></label><button class="secondary-button" type="button" :disabled="saving === group.group_id" @click="saveDraft(group)">{{ saving === group.group_id ? '保存并校验中…' : '保存并重新校验' }}</button></div></td></tr>
            <tr v-if="result[groupKey(group)]" class="batch-details"><td colspan="7"><NoticeBanner tone="info" :message="result[groupKey(group)]" /></td></tr>
            <tr v-if="!group.task_id && candidateForms[groupKey(group)]" class="batch-details"><td colspan="7"><div class="batch-edit-grid"><label>交期<input v-model="candidateForms[groupKey(group)].delivery_date" type="date"></label><label>收货地址<input v-model="candidateForms[groupKey(group)].address"></label><label>联系人<input v-model="candidateForms[groupKey(group)].contact"></label><label v-for="(item, index) in candidateForms[groupKey(group)].items" :key="`raw-${index}`">物料名称<input v-model="item.raw_name"></label><label v-for="(item, index) in candidateForms[groupKey(group)].items" :key="`spec-${index}`">规格<input v-model="item.specification"></label><label v-for="(item, index) in candidateForms[groupKey(group)].items" :key="`quantity-${index}`">数量<input v-model="item.quantity"></label><label v-for="(item, index) in candidateForms[groupKey(group)].items" :key="`unit-${index}`">单位<input v-model="item.unit"></label><label v-for="(item, index) in candidateForms[groupKey(group)].items" :key="`price-${index}`">单价<input v-model="item.unit_price"></label><button class="secondary-button" type="button" :disabled="saving === groupKey(group)" @click="saveCorrection(group)">保存并重新校验</button></div></td></tr>
          </tbody>
        </table></div>
        <div v-if="batch.groups.length === 0" class="batch-empty">该批次没有订单组。</div>
        <NoticeBanner tone="warning" message="审批前服务端仍会逐笔复核当前权限、草稿版本、主数据和实时额度；部分成功不会回滚已生成订单。模型输出和 Excel 内容都不构成人工批准。" />
        <footer class="batch-footer"><span>已选 {{ selectedGroups.length }} 笔 · 含税金额 {{ formatMoney(String(selectedAmount)) }}</span><button v-if="canApprove" class="primary-button" type="button" :disabled="approving || !selectedGroups.length" @click="openApprovalReview">核对所选订单</button></footer>
      </section>
      <div v-if="retryTarget" class="batch-modal-backdrop" role="presentation" @click.self="retryTarget = null">
        <section class="batch-modal" role="dialog" aria-modal="true" aria-labelledby="batch-retry-title">
          <header><div><p class="eyebrow">安全恢复</p><h2 id="batch-retry-title">恢复原任务的失败步骤</h2><p>{{ retryTarget.group_id }} · 任务 {{ retryTarget.task_id?.slice(0, 8) }}</p></div><button class="icon-button" type="button" aria-label="关闭" @click="retryTarget = null">×</button></header>
          <p>失败位置：{{ failureName(retryTarget.task_id ? tasks[retryTarget.task_id] : undefined) }} · {{ tasks[retryTarget.task_id!]?.last_error?.code ?? '未知错误' }}</p>
          <NoticeBanner tone="warning" message="系统会先刷新并确认失败位置未变化，再按原任务 checkpoint 恢复。不可重试的规则阻断不会开放此操作；不会新建任务或直接创建订单。" />
          <footer><button class="secondary-button" type="button" :disabled="Boolean(retrying)" @click="retryTarget = null">返回批次</button><button class="primary-button" type="button" :disabled="Boolean(retrying)" @click="confirmRetry">{{ retrying ? '正在核实并恢复…' : '确认安全恢复' }}</button></footer>
        </section>
      </div>
      <div v-if="reviewConfirmOpen" class="batch-modal-backdrop" role="presentation" @click.self="reviewConfirmOpen = false">
        <section class="batch-modal" role="dialog" aria-modal="true" aria-labelledby="batch-approval-title">
          <header><div><p class="eyebrow">逐笔版本确认</p><h2 id="batch-approval-title">确认批准所选 {{ selectedGroups.length }} 笔订单</h2><p>每笔决定独立提交，只批准下方当前展示的版本。</p></div><button class="icon-button" type="button" aria-label="关闭" @click="reviewConfirmOpen = false">×</button></header>
          <div class="batch-approval-list"><article v-for="group in selectedGroups" :key="groupKey(group)"><strong>{{ group.group_id }} / {{ group.task_id?.slice(0, 8) }}</strong><span>合同 {{ tasks[group.task_id!]?.draft?.candidate?.contract_id }}</span><span>版本 v{{ tasks[group.task_id!]?.draft?.version }} · 含税金额 {{ formatMoney(tasks[group.task_id!]?.draft?.total_amount) }}</span><span>当前可用余额 {{ formatMoney(tasks[group.task_id!]?.quota_preview?.remaining_amount) }}</span><small>摘要 {{ tasks[group.task_id!]?.draft?.content_hash }}</small><small>审批前会再次核验实时额度与权限。</small></article></div>
          <p class="batch-modal-total">所选含税合计 <strong>{{ formatMoney(String(selectedAmount)) }}</strong></p>
          <NoticeBanner tone="warning" message="操作会按每笔订单分别写入当前审批人和版本快照；部分成功时已提交项目不会回滚或重复创建。" />
          <footer><button class="secondary-button" type="button" :disabled="approving" @click="reviewConfirmOpen = false">返回核对</button><button class="primary-button" type="button" :disabled="approving || !selectedGroups.length" @click="approveSelected">{{ approving ? '正在逐笔提交…' : `批准 ${selectedGroups.length} 笔并提单` }}</button></footer>
        </section>
      </div>
    </template>
  </section>
</template>

<style scoped>
.batch-page { display: grid; gap: 16px; }
.batch-metrics { display: grid; grid-template-columns: repeat(8,minmax(0,1fr)); gap: 12px; }
.batch-metrics article,.batch-card { padding: 17px; border: 1px solid var(--color-border); border-radius: 8px; background: #fff; }
.batch-metrics article { display: grid; gap: 7px; color: var(--color-muted); font-size: var(--font-size-sm); }
.batch-metrics strong { color: #193344; font-size: var(--font-size-stat); }
.batch-card { display: grid; gap: 16px; }
.batch-card__heading,.batch-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.batch-card__heading h2 { margin: 0 0 5px; font-size: var(--font-size-lg); }
.batch-card__heading p,.batch-table small { margin: 0; color: var(--color-muted); font-size: var(--font-size-sm); }
.batch-table-wrap { overflow-x: auto; }
.batch-table { width: 100%; border-collapse: collapse; text-align: left; font-size: var(--font-size-sm); }
.batch-table th { padding: 11px 9px; background: #f3f6f8; color: var(--color-muted); font-size: var(--font-size-sm); font-weight: 500; white-space: nowrap; }
.batch-table td { padding: 13px 9px; border-bottom: 1px solid #e8edf1; vertical-align: top; }
.batch-table td small { display: block; margin-top: 5px; }
.batch-select { display: inline-flex; align-items: center; gap: 7px; }
.batch-select input { accent-color: var(--color-primary); }
.batch-row--blocked { background: #fffafa; }
.batch-row--blocked td { border-bottom-color: #f3e6e6; }
.batch-table code { color: #31566b; font-family: inherit; }
.batch-link { color: var(--color-primary); }
.batch-row-actions { display: grid; justify-items: start; gap: 6px; }
.batch-details td { padding-top: 0; background: #fbfcfd; }
.batch-details ul { display: grid; gap: 6px; margin: 8px 0; padding-left: 20px; color: var(--color-danger); }
.batch-details > td > small { color: var(--color-muted); }
.batch-edit-grid { display: grid; grid-template-columns: repeat(3,minmax(130px,1fr)); align-items: end; gap: 10px; padding: 12px 0; }
.batch-edit-grid label { display: grid; gap: 5px; color: var(--color-muted); font-size: var(--font-size-sm); }
.batch-edit-grid input { min-height: 37px; padding: 0 8px; border: 1px solid var(--color-border); border-radius: 5px; color: #263d4d; }
.batch-footer { padding-top: 10px; color: var(--color-muted); font-size: var(--font-size-sm); }
.batch-empty { padding: 35px; color: var(--color-muted); text-align: center; }
.batch-modal-backdrop { position: fixed; z-index: 30; inset: 0; display: grid; place-items: center; padding: 20px; background: rgb(16 35 47 / 38%); }
.batch-modal { display: grid; width: min(100%,760px); max-height: 90vh; gap: 16px; overflow: auto; padding: 25px; border-radius: 9px; background: #fff; box-shadow: 0 20px 60px rgb(14 39 52 / 22%); }
.batch-modal > header { display: flex; justify-content: space-between; gap: 16px; }
.batch-modal h2 { margin: 0 0 5px; font-size: var(--font-size-xl); }
.batch-modal header p:last-child { margin: 0; color: var(--color-muted); font-size: var(--font-size-sm); }
.batch-approval-list { display: grid; max-height: 42vh; gap: 8px; overflow: auto; }
.batch-approval-list article { display: grid; grid-template-columns: 1fr 1fr; gap: 7px 12px; padding: 12px; border-bottom: 1px solid #e7edf1; font-size: var(--font-size-sm); }
.batch-approval-list strong { grid-column: 1 / -1; }
.batch-approval-list small { overflow-wrap: anywhere; color: var(--color-muted); }
.batch-modal-total { display: flex; align-items: center; justify-content: flex-end; gap: 12px; margin: 0; color: var(--color-muted); font-size: var(--font-size-sm); }
.batch-modal-total strong { color: #193344; font-size: var(--font-size-page); }
.batch-modal > footer { display: flex; justify-content: flex-end; gap: 9px; }
@media (max-width: 900px) { .batch-metrics { grid-template-columns: repeat(2,minmax(0,1fr)); } .batch-edit-grid { grid-template-columns: repeat(2,minmax(120px,1fr)); } }
</style>
