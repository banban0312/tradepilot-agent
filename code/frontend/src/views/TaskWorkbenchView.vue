<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { useAuthStore } from '../stores/auth'
import { artifactsApi, type Artifact, type ParseResult, type ParsedField } from '../services/artifacts'
import { importsApi, type ImportPreview } from '../services/imports'
import { tasksApi, TASK_EVENT_TYPES, type DraftCandidate, type FailedTaskSpan, type FileFieldConfirmation, type MaterialCandidate, type MaterialMatchResult, type OrderType, type TaskSnapshot, type TaskState, type TaskSummary } from '../services/tasks'

type WorkEvent = { type: string; seq: number; emitted_at: string; data: Record<string, unknown> }
type InputMode = 'text' | 'document' | 'excel'
type DocumentEntry = {
  artifact: Artifact
  result: ParseResult | null
  previewUrl: string
  status: 'uploaded' | 'parsing' | 'parsed' | 'failed'
  message: string
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const orderType = ref<OrderType>('SALES')
const newTaskText = ref('')
const inputMode = ref<InputMode>('text')
const documents = ref<DocumentEntry[]>([])
const pendingDocumentIds = ref<string[]>([])
const taskArtifacts = ref<Artifact[]>([])
const activeArtifactId = ref('')
const excelArtifactId = ref('')
const excelPreview = ref<ImportPreview | null>(null)
const excelStatus = ref('')
const fileBusy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const evidenceSelections = ref<Record<string, string>>({})
const applyingEvidence = ref(false)
const replyText = ref('')
const taskList = ref<TaskSummary[]>([])
const task = ref<TaskSnapshot | null>(null)
const draftForm = ref<DraftCandidate | null>(null)
const materialMatches = ref<Record<number, MaterialMatchResult>>({})
const matchingItems = ref(new Set<number>())
const materialDecisions = ref(new Set<string>())
const savedForm = ref('')
const activity = ref<WorkEvent[]>([])
const focusedFailureSpan = ref<FailedTaskSpan | null>(null)
const focusedFailureError = ref('')
const loadingFailureSpan = ref(false)
const loading = ref(false)
const sending = ref(false)
const creatingBatch = ref(false)
const saving = ref(false)
const reviewing = ref(false)
const errorMessage = ref('')
const noticeMessage = ref('')
const reviewDialog = ref<null | 'RETURN' | 'REJECT'>(null)
const reviewReason = ref('')
const keepLocalDraft = ref(false)
let eventSource: EventSource | null = null
let taskGeneration = 0
let failureSpanGeneration = 0

const pageMode = computed(() => typeof route.params.taskId === 'string' ? 'task' : 'home')
const isDirty = computed(() => Boolean(draftForm.value && JSON.stringify(editableCandidate(draftForm.value)) !== savedForm.value))
const canReply = computed(() => Boolean(task.value?.active_wait?.kind === 'INPUT' && task.value.available_actions.includes('INPUT')))
const canEditDraft = computed(() => Boolean(task.value?.available_actions.includes('EDIT_DRAFT') && task.value.draft?.candidate))
const canApprove = computed(() => Boolean(
  task.value?.active_wait?.kind === 'APPROVAL'
  && task.value.available_actions.includes('APPROVE')
  && auth.identity?.roles.includes('business_approver'),
))
const activeDocument = computed(() => documents.value.find(item => item.artifact.artifact_id === activeArtifactId.value) ?? null)
const activeEvidencePage = computed(() => {
  if (!activeDocument.value?.result) return 1
  for (const [index, field] of activeDocument.value.result.fields.entries()) {
    if (evidenceSelections.value[field.field] === evidenceFieldId(field, index)) return field.page_number
  }
  return activeDocument.value.result.fields[0]?.page_number ?? 1
})
const canUploadTaskFile = computed(() => Boolean(
  task.value && canEditDraft.value && ['QUEUED', 'WAITING_INPUT', 'BLOCKED_RULE'].includes(task.value.state),
))
const selectedEvidenceCount = computed(() => Object.values(evidenceSelections.value).filter(Boolean).length)
const stateLabels: Record<TaskState, string> = {
  QUEUED: '排队中', RUNNING: '处理中', WAITING_INPUT: '等待补充', WAITING_APPROVAL: '等待审批',
  SUBMITTING: '正在提交', BLOCKED_RULE: '规则阻断', FAILED: '处理失败', COMPLETED: '已完成', REJECTED: '已拒绝',
}
const stateTone = computed(() => {
  if (!task.value) return 'info'
  if (task.value.state === 'FAILED' || task.value.state === 'BLOCKED_RULE' || task.value.state === 'REJECTED') return 'danger'
  if (task.value.state === 'WAITING_INPUT' || task.value.state === 'WAITING_APPROVAL') return 'warning'
  if (task.value.state === 'COMPLETED') return 'success'
  return 'info'
})
const draftIssues = computed(() => task.value?.draft?.issues ?? [])
const missingLabels = computed(() => task.value?.active_wait?.missing_fields.map(fieldLabel) ?? [])
const quota = computed(() => task.value?.quota_preview)
const quotaUnavailable = computed(() => !quota.value?.balance_available)

function centsFromDecimal(value: string | null | undefined): bigint | null {
  if (value === null || value === undefined) return null
  const match = value.trim().match(/^(\d+)(?:\.(\d*))?$/)
  if (!match) return null
  const fraction = match[2] ?? ''
  let cents = BigInt(match[1]) * 100n + BigInt((fraction + '00').slice(0, 2))
  if (fraction.length > 2 && fraction[2] >= '5') cents += 1n
  return cents
}

function microsFromDecimal(value: string | null): bigint | null {
  if (value === null) return null
  const match = value.trim().match(/^(\d+)(?:\.(\d{0,6}))?$/)
  if (!match) return null
  return BigInt(match[1]) * 1_000_000n + BigInt((match[2] ?? '').padEnd(6, '0'))
}

function centsToDecimal(cents: bigint): string {
  const whole = cents / 100n
  const fraction = (cents % 100n).toString().padStart(2, '0')
  return `${whole}.${fraction}`
}

function lineAmount(item: DraftCandidate['items'][number]): string | null {
  const quantity = microsFromDecimal(item.quantity)
  const unitPrice = microsFromDecimal(item.unit_price)
  if (quantity === null || unitPrice === null) return null
  // 数量与单价各保留六位小数，按后端 ROUND_HALF_UP 规则折算到分。
  const divisor = 1_000_000_000_000n
  return centsToDecimal((quantity * unitPrice * 100n + divisor / 2n) / divisor)
}

const previewTotal = computed(() => {
  if (!draftForm.value?.items.length) return null
  let sum = 0n
  for (const item of draftForm.value.items) {
    const amount = lineAmount(item)
    if (amount === null) return null
    const cents = centsFromDecimal(amount)
    if (cents === null) return null
    sum += cents
  }
  return centsToDecimal(sum)
})
const totalAmount = computed(() => isDirty.value ? previewTotal.value : task.value?.draft?.total_amount ?? null)
const quotaRequested = computed(() => isDirty.value ? previewTotal.value : quota.value?.requested_amount ?? null)
const quotaExcess = computed(() => {
  if (!quota.value) return null
  if (!isDirty.value) return quota.value.excess_amount
  const requested = centsFromDecimal(quotaRequested.value)
  const remaining = centsFromDecimal(quota.value.remaining_amount)
  if (requested === null || remaining === null) return null
  return centsToDecimal(requested > remaining ? requested - remaining : 0n)
})
const shouldBlockByQuota = computed(() => (centsFromDecimal(quotaExcess.value) ?? 0n) > 0n)
const quotaRemaining = computed(() => {
  const remaining = centsFromDecimal(quota.value?.remaining_amount)
  if (remaining === null) return null
  if (!isDirty.value) return centsToDecimal(remaining)
  const preview = centsFromDecimal(previewTotal.value)
  const requested = centsFromDecimal(quota.value?.requested_amount)
  if (preview === null || requested === null) return null
  return centsToDecimal(remaining + requested > preview ? remaining + requested - preview : 0n)
})
const quotaPercent = computed(() => {
  const contract = centsFromDecimal(quota.value?.contract_amount)
  const remaining = centsFromDecimal(quotaRemaining.value)
  if (contract === null || contract <= 0n || remaining === null) return 0
  return Math.min(100, Math.max(0, Number(remaining) / Number(contract) * 100))
})

function editableCandidate(candidate: DraftCandidate) {
  return {
    contract_id: candidate.contract_id,
    customer_id: candidate.customer_id,
    project_id: candidate.project_id,
    currency: candidate.currency,
    delivery_date: candidate.delivery_date,
    address: candidate.address,
    contact: candidate.contact,
    business_fields: candidate.business_fields,
    items: candidate.items,
  }
}

function fieldLabel(field: string) {
  const labels: Record<string, string> = {
    order_type: '订单类型', contract_id: '合同', customer_id: '客户', project_id: '项目', currency: '币种',
    delivery_date: '交期', address: '收货地址', contact: '联系人', total_amount: '订单金额',
  }
  const itemMatch = field.match(/^items\[(\d+)](?:\.(.+))?$/)
  if (itemMatch) {
    const itemName: Record<string, string> = { raw_name: '物料名称', specification: '规格', quantity: '数量', unit: '单位', unit_price: '单价' }
    return `第 ${Number(itemMatch[1]) + 1} 项${itemMatch[2] ? ` · ${itemName[itemMatch[2]] ?? itemMatch[2]}` : ''}`
  }
  return labels[field] ?? field
}

function formatMoney(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') return '待核算'
  const cents = centsFromDecimal(String(value))
  if (cents === null) return '待核算'
  const sign = cents < 0n ? '-' : ''
  const absolute = cents < 0n ? -cents : cents
  return `${sign}${(absolute / 100n).toLocaleString('zh-CN')}.${(absolute % 100n).toString().padStart(2, '0')}`
}

function formatTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', month: '2-digit', day: '2-digit' }).format(date)
}

function syncFormFromTask(snapshot: TaskSnapshot, force = false) {
  const candidate = snapshot.draft?.candidate
  if (!candidate) {
    draftForm.value = null
    savedForm.value = ''
    return
  }
  if (!force && isDirty.value) {
    keepLocalDraft.value = true
    return
  }
  materialMatches.value = {}
  matchingItems.value.clear()
  materialDecisions.value.clear()
  draftForm.value = structuredClone(candidate)
  savedForm.value = JSON.stringify(editableCandidate(draftForm.value))
  keepLocalDraft.value = false
}

async function refreshSnapshot(generation = taskGeneration) {
  if (generation !== taskGeneration || typeof route.params.taskId !== 'string') return
  try {
    const snapshot = await tasksApi.get(route.params.taskId)
    if (generation !== taskGeneration) return
    const previousOrderId = task.value?.order_ref?.order_id
    task.value = snapshot
    if (!previousOrderId && snapshot.order_ref?.order_id) {
      window.dispatchEvent(new CustomEvent('tradepilot:order-created', { detail: { orderId: snapshot.order_ref.order_id } }))
    }
    syncFormFromTask(snapshot)
  } catch (error) {
    if (generation === taskGeneration) errorMessage.value = error instanceof Error ? error.message : '读取任务失败。'
  }
}

async function loadFocusedFailureSpan() {
  const generation = ++failureSpanGeneration
  const taskId = route.params.taskId
  const spanId = route.query.failed_span_id
  focusedFailureSpan.value = null
  focusedFailureError.value = ''
  if (typeof taskId !== 'string' || typeof spanId !== 'string') return
  loadingFailureSpan.value = true
  try {
    const span = await tasksApi.getFailedSpan(taskId, spanId)
    if (generation === failureSpanGeneration) focusedFailureSpan.value = span
  } catch (caught) {
    if (generation === failureSpanGeneration) {
      focusedFailureError.value = caught instanceof Error ? caught.message : '无法读取该失败轨迹。'
    }
  } finally {
    if (generation === failureSpanGeneration) loadingFailureSpan.value = false
  }
}

function openCreatedOrder() {
  const orderId = task.value?.order_ref?.order_id
  if (!orderId) return
  void router.push({ name: 'order-center', query: { order_id: orderId } })
}

function stopEvents() {
  eventSource?.close()
  eventSource = null
}

function connectEvents(taskId: string, generation: number) {
  stopEvents()
  // 首次连接不带游标时服务端从 0 重放可见历史；重连由浏览器携带 Last-Event-ID 续接。
  const source = new EventSource(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`)
  eventSource = source
  const receive = (event: Event) => {
    if (generation !== taskGeneration) return
    const message = event as MessageEvent<string>
    try {
      const envelope = JSON.parse(message.data) as { seq?: number; emitted_at?: string; data?: Record<string, unknown> }
      const payload = envelope.data ?? {}
      const eventData = payload.data && typeof payload.data === 'object'
        ? payload.data as Record<string, unknown>
        : payload
      activity.value = [{
        type: event.type,
        seq: envelope.seq ?? 0,
        emitted_at: envelope.emitted_at ?? new Date().toISOString(),
        data: eventData,
      }, ...activity.value].slice(0, 12)
    } catch {
      activity.value = [{ type: event.type, seq: 0, emitted_at: new Date().toISOString(), data: {} }, ...activity.value].slice(0, 12)
    }
    void refreshSnapshot(generation)
  }
  for (const type of TASK_EVENT_TYPES) source.addEventListener(type, receive)
  source.onerror = () => {
    // EventSource 会按 Last-Event-ID 自动重连；先重新读取权威快照，再接收后续事件。
    void refreshSnapshot(generation)
  }
}

async function loadTask(taskId: string) {
  const generation = ++taskGeneration
  stopEvents()
  activity.value = []
  errorMessage.value = ''
  loading.value = true
  task.value = null
  draftForm.value = null
  try {
    const snapshot = await tasksApi.get(taskId)
    if (generation !== taskGeneration) return
    task.value = snapshot
    syncFormFromTask(snapshot, true)
    await loadTaskArtifacts(taskId)
    connectEvents(taskId, generation)
  } catch (error) {
    if (generation === taskGeneration) errorMessage.value = error instanceof Error ? error.message : '读取任务失败。'
  } finally {
    if (generation === taskGeneration) loading.value = false
  }
}

watch(() => route.params.taskId, (value) => {
  if (typeof value === 'string') void loadTask(value)
  else {
    taskGeneration += 1
    stopEvents()
    task.value = null
    draftForm.value = null
    taskArtifacts.value = []
    activeArtifactId.value = ''
    pendingDocumentIds.value = []
    errorMessage.value = ''
  }
}, { immediate: true })

watch([() => route.params.taskId, () => route.query.failed_span_id], () => {
  void loadFocusedFailureSpan()
}, { immediate: true })

watch(() => task.value?.draft?.version, async (version, previous) => {
  if (version && previous && version !== previous && isDirty.value && !saving.value) {
    keepLocalDraft.value = true
    await nextTick()
  }
})

onBeforeUnmount(() => {
  stopEvents()
  for (const entry of documents.value) {
    if (entry.previewUrl) URL.revokeObjectURL(entry.previewUrl)
  }
})

function entryFor(artifact: Artifact) {
  let entry = documents.value.find(item => item.artifact.artifact_id === artifact.artifact_id)
  if (!entry) {
    entry = { artifact, result: null, previewUrl: '', status: 'uploaded', message: '' }
    documents.value.push(entry)
  } else {
    entry.artifact = artifact
  }
  return entry
}

async function loadArtifactPreview(entry: DocumentEntry) {
  if (entry.previewUrl) return
  const content = await artifactsApi.content(entry.artifact.artifact_id)
  entry.previewUrl = URL.createObjectURL(content)
}

async function parseDocument(entry: DocumentEntry, force = false) {
  if (entry.status === 'parsing' || (entry.result && !force)) return
  entry.status = 'parsing'
  entry.message = ''
  try {
    entry.result = await artifactsApi.parse(entry.artifact.artifact_id)
    entry.status = 'parsed'
    evidenceSelections.value = {}
  } catch (error) {
    entry.status = 'failed'
    entry.message = error instanceof Error ? error.message : '文件解析失败。'
  }
  try {
    await loadArtifactPreview(entry)
  } catch (error) {
    if (!entry.message) entry.message = error instanceof Error ? error.message : '原文件预览加载失败。'
  }
}

function fileIntent(file: File) {
  const extension = file.name.split('.').pop()?.toLowerCase()
  if (extension === 'pdf') return 'contract_pdf' as const
  if (['png', 'jpg', 'jpeg'].includes(extension ?? '')) return 'receipt_image' as const
  if (extension === 'xlsx') return 'order_xlsx' as const
  throw new Error('仅支持 PDF、PNG、JPG 和 XLSX 文件。')
}

async function processExcel(file?: File) {
  if (!excelArtifactId.value) {
    if (!file) throw new Error('请重新选择尚未上传的 Excel 文件。')
    excelStatus.value = '正在安全上传 Excel…'
    excelArtifactId.value = await importsApi.upload(file)
  }
  excelStatus.value = '正在校验工作表、分组和单元格…'
  excelPreview.value = { ...(await importsApi.previewArtifact(excelArtifactId.value)), artifact_id: excelArtifactId.value }
  excelStatus.value = '分组校验完成；尚未创建订单或提交审批。'
}

async function createExcelBatch() {
  if (!excelArtifactId.value || creatingBatch.value || !excelPreview.value?.groups.length) return
  creatingBatch.value = true
  errorMessage.value = ''
  try {
    const batch = await importsApi.createBatch(excelArtifactId.value)
    await router.push({ name: 'import-batch', params: { batchId: batch.batch_id } })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建导入批次失败。'
  } finally {
    creatingBatch.value = false
  }
}

async function retryExcelPreview() {
  fileBusy.value = true
  errorMessage.value = ''
  try { await processExcel() }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : 'Excel 分组校验失败。' }
  finally { fileBusy.value = false }
}

async function handleFiles(files: FileList | File[]) {
  const file = files[0]
  if (!file || fileBusy.value) return
  fileBusy.value = true
  errorMessage.value = ''
  try {
    const intent = fileIntent(file)
    if (pageMode.value === 'home' && inputMode.value === 'excel' && intent !== 'order_xlsx') {
      throw new Error('Excel 面板只接受 .xlsx 文件。')
    }
    if (pageMode.value === 'home' && inputMode.value !== 'excel' && intent === 'order_xlsx') {
      throw new Error('请切换到 Excel 面板上传 .xlsx 文件。')
    }
    if (intent === 'order_xlsx') {
      if (pageMode.value !== 'home') throw new Error('Excel 文件请在新任务页进行分组校验。')
      if (file.size > 10 * 1024 * 1024) throw new Error('Excel 文件不能超过 10 MB。')
      excelPreview.value = null
      excelArtifactId.value = ''
      await processExcel(file)
      return
    }
    const limit = intent === 'contract_pdf' ? 20 * 1024 * 1024 : 10 * 1024 * 1024
    if (file.size > limit) throw new Error(`文件不能超过 ${intent === 'contract_pdf' ? '20' : '10'} MB。`)
    if (pageMode.value === 'task' && (!task.value || !canUploadTaskFile.value)) {
      throw new Error('任务当前不接受附件；请等待补充信息或规则修正步骤。')
    }
    const artifact = await artifactsApi.upload(file, intent, pageMode.value === 'task' ? task.value?.task_id : undefined)
    const entry = entryFor(artifact)
    activeArtifactId.value = artifact.artifact_id
    if (pageMode.value === 'home') pendingDocumentIds.value.push(artifact.artifact_id)
    else taskArtifacts.value = [...taskArtifacts.value, artifact]
    await parseDocument(entry)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文件处理失败。'
  } finally {
    fileBusy.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files) void handleFiles(input.files)
}

function onFileDrop(event: DragEvent) {
  event.preventDefault()
  if (event.dataTransfer?.files) void handleFiles(event.dataTransfer.files)
}

async function inspectArtifact(artifact: Artifact) {
  if (activeArtifactId.value !== artifact.artifact_id) evidenceSelections.value = {}
  activeArtifactId.value = artifact.artifact_id
  const entry = entryFor(artifact)
  if (!entry.result) await parseDocument(entry)
  else {
    try { await loadArtifactPreview(entry) }
    catch (error) { entry.message = error instanceof Error ? error.message : '原文件预览加载失败。' }
  }
}

async function loadTaskArtifacts(taskId: string) {
  try {
    taskArtifacts.value = await artifactsApi.listTask(taskId)
    for (const artifact of taskArtifacts.value) entryFor(artifact)
    if (!taskArtifacts.value.some(item => item.artifact_id === activeArtifactId.value)) {
      activeArtifactId.value = taskArtifacts.value[0]?.artifact_id ?? ''
      evidenceSelections.value = {}
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取任务附件失败。'
  }
}

function fieldLabelForEvidence(field: string) {
  const labels: Record<string, string> = {
    contract_number: '合同编号', customer: '客户名称', material: '物料名称',
    specification: '规格型号', quantity: '数量', amount: '合同金额',
    delivery_date: '交期', recipient: '联系人', delivery_address: '收货地址',
  }
  return labels[field] ?? field
}

function fieldPathForEvidence(field: ParsedField) {
  const direct: Record<string, string> = {
    delivery_date: 'delivery_date', delivery_address: 'address', recipient: 'contact',
    material: 'items[0].raw_name', specification: 'items[0].specification',
    quantity: 'items[0].quantity',
  }
  return direct[field.field] ?? null
}

function evidenceFieldId(field: ParsedField, index: number) {
  return `${activeArtifactId.value}:${field.field}:${field.page_number}:${index}`
}

function normalizeEvidenceValue(field: ParsedField) {
  if (field.field !== 'delivery_date') return field.value
  const match = field.value.match(/^(\d{4})[年./-]\s*(\d{1,2})[月./-]\s*(\d{1,2})日?$/)
  return match ? `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}` : field.value
}

function selectedFileConfirmations(): FileFieldConfirmation[] {
  if (!activeDocument.value?.result) return []
  const confirmations: FileFieldConfirmation[] = []
  for (const [index, field] of activeDocument.value.result.fields.entries()) {
    if (evidenceSelections.value[field.field] !== evidenceFieldId(field, index)) continue
    const value = normalizeEvidenceValue(field)
    const paths = field.field === 'quantity' && field.unit
      ? [{ field_path: 'items[0].quantity', value }, { field_path: 'items[0].unit', value: field.unit }]
      : [{ field_path: fieldPathForEvidence(field)!, value }]
    for (const mapped of paths) {
      confirmations.push({
        field_path: mapped.field_path,
        artifact_id: activeDocument.value.artifact.artifact_id,
        page_number: field.page_number,
        locator: field.locator,
        raw_value: field.raw_text,
      })
    }
  }
  return confirmations
}

async function applySelectedEvidence() {
  if (!task.value || !draftForm.value || !canEditDraft.value || isDirty.value || applyingEvidence.value) return
  const confirmations = selectedFileConfirmations()
  if (!confirmations.length) return
  const updated = structuredClone(draftForm.value)
  for (const [index, field] of activeDocument.value!.result!.fields.entries()) {
    if (evidenceSelections.value[field.field] !== evidenceFieldId(field, index)) continue
    const mappings = field.field === 'quantity' && field.unit
      ? [{ path: 'items[0].quantity', value: normalizeEvidenceValue(field) }, { path: 'items[0].unit', value: field.unit }]
      : [{ path: fieldPathForEvidence(field)!, value: normalizeEvidenceValue(field) }]
    if (!updated.items.length && mappings.some(mapping => mapping.path.startsWith('items['))) {
      updated.items.push({ material_id: null, raw_name: null, specification: null, quantity: null, unit: null, unit_price: null })
    }
    for (const mapping of mappings) {
      if (mapping.path === 'delivery_date') updated.delivery_date = mapping.value
      else if (mapping.path === 'address') updated.address = mapping.value
      else if (mapping.path === 'contact') updated.contact = mapping.value
      else {
        const match = mapping.path.match(/^items\[0\]\.(.+)$/)
        if (match) updated.items[0][match[1] as keyof typeof updated.items[number]] = mapping.value as never
      }
    }
  }
  applyingEvidence.value = true
  saving.value = true
  errorMessage.value = ''
  draftForm.value = updated
  try {
    await tasksApi.patchDraft(task.value, cleanCandidate(updated), confirmations)
    savedForm.value = JSON.stringify(editableCandidate(updated))
    keepLocalDraft.value = false
    evidenceSelections.value = {}
    noticeMessage.value = '字段确认已记录，系统正在重新校验；草稿内容变化时会生成新版本，该版本仍需人工审批。'
    await refreshSnapshot()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '应用确认字段失败。'
    if ((error as { status?: number }).status === 409) {
      keepLocalDraft.value = true
      await refreshSnapshot()
    }
  } finally {
    saving.value = false
    applyingEvidence.value = false
  }
}

async function loadRecentTasks() {
  loading.value = true
  errorMessage.value = ''
  try {
    const response = await tasksApi.list()
    taskList.value = response.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取最近任务失败。'
  } finally {
    loading.value = false
  }
}

watch(pageMode, (mode) => { if (mode === 'home') void loadRecentTasks() }, { immediate: true })

async function createTask() {
  const text = newTaskText.value.trim()
  if (!text || sending.value) return
  sending.value = true
  errorMessage.value = ''
  try {
    const created = await tasksApi.create(orderType.value, text, pendingDocumentIds.value)
    newTaskText.value = ''
    pendingDocumentIds.value = []
    await router.push({ name: 'task-workbench', params: { taskId: created.task_id } })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '任务创建失败。'
  } finally {
    sending.value = false
  }
}

async function continueLastTask() {
  if (!taskList.value.length) await loadRecentTasks()
  const latest = taskList.value[0]
  if (latest) await router.push({ name: 'task-workbench', params: { taskId: latest.task_id } })
  else noticeMessage.value = '当前账号还没有可继续的任务。'
}

async function sendReply() {
  const text = replyText.value.trim()
  if (!task.value || !text || sending.value || !canReply.value) return
  sending.value = true
  errorMessage.value = ''
  try {
    await tasksApi.supplement(task.value, text)
    replyText.value = ''
    noticeMessage.value = '补充内容已接收，任务已进入队列；后续状态以服务端更新为准。'
    await refreshSnapshot()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补充信息失败。'
    if ((error as { status?: number }).status === 409) await refreshSnapshot()
  } finally {
    sending.value = false
  }
}

function cleanCandidate(candidate: DraftCandidate) {
  const value = structuredClone(editableCandidate(candidate))
  value.delivery_date = value.delivery_date || null
  value.address = value.address || null
  value.contact = value.contact || null
  value.items = value.items.map((item) => ({
    material_id: item.material_id || null,
    raw_name: item.raw_name || null,
    specification: item.specification || null,
    quantity: item.quantity || null,
    unit: item.unit || null,
    unit_price: item.unit_price || null,
  }))
  return value
}

async function saveDraft() {
  if (!task.value || !draftForm.value || !canEditDraft.value || !isDirty.value || saving.value) return
  const submitted = structuredClone(draftForm.value)
  saving.value = true
  errorMessage.value = ''
  try {
    await tasksApi.patchDraft(task.value, cleanCandidate(submitted))
    savedForm.value = JSON.stringify(editableCandidate(submitted))
    keepLocalDraft.value = false
    noticeMessage.value = '草稿修改已接收，系统正在重新校验；当前版本尚未通过审批。'
    await refreshSnapshot()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '草稿保存失败。'
    if ((error as { status?: number }).status === 409) {
      keepLocalDraft.value = true
      await refreshSnapshot()
    }
  } finally {
    saving.value = false
  }
}

async function findMaterialMatches(index: number) {
  if (!task.value || !canEditDraft.value || isDirty.value || matchingItems.value.has(index)) return
  matchingItems.value.add(index)
  errorMessage.value = ''
  try {
    materialMatches.value[index] = await tasksApi.matchMaterials(task.value, index)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '物料候选查询失败。'
    if ((error as { status?: number }).status === 409) await refreshSnapshot()
  } finally {
    matchingItems.value.delete(index)
  }
}

async function acceptMaterialCandidate(index: number, candidate: MaterialCandidate) {
  if (!draftForm.value || !canEditDraft.value || saving.value || isDirty.value) return
  const item = draftForm.value.items[index]
  if (!item) return
  item.material_id = candidate.material_id
  item.specification = candidate.specification
  item.unit = candidate.unit
  await nextTick()
  await saveDraft()
}

async function rejectMaterialCandidate(index: number, candidate: MaterialCandidate) {
  if (!task.value || !canEditDraft.value || isDirty.value) return
  const key = `${index}:${candidate.material_id}`
  if (materialDecisions.value.has(key)) return
  materialDecisions.value.add(key)
  try {
    await tasksApi.rejectMaterial(task.value, index, candidate.material_id)
    noticeMessage.value = `已记录拒绝候选 ${candidate.code}。草稿未被修改，仍需选择其他候选或补充规格。`
  } catch (error) {
    materialDecisions.value.delete(key)
    errorMessage.value = error instanceof Error ? error.message : '记录物料决定失败。'
    if ((error as { status?: number }).status === 409) await refreshSnapshot()
  }
}

function addItem() {
  if (!draftForm.value) return
  draftForm.value.items.push({ material_id: null, raw_name: null, specification: null, quantity: null, unit: null, unit_price: null })
}

function removeItem(index: number) {
  draftForm.value?.items.splice(index, 1)
}

async function decide(decision: 'APPROVE' | 'RETURN' | 'REJECT', reason?: string) {
  if (!task.value || reviewing.value) return
  reviewing.value = true
  errorMessage.value = ''
  try {
    await tasksApi.review(task.value, decision, reason)
    reviewDialog.value = null
    reviewReason.value = ''
    noticeMessage.value = '审批决定已登记并排入任务队列；202 仅代表已受理，不能据此判断订单已创建。'
    await refreshSnapshot()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审批决定未能提交。'
    if ((error as { status?: number }).status === 409) await refreshSnapshot()
  } finally {
    reviewing.value = false
  }
}

function openReviewDialog(decision: 'RETURN' | 'REJECT') {
  reviewDialog.value = decision
  reviewReason.value = ''
}

async function reloadServerDraft() {
  if (typeof route.params.taskId !== 'string') return
  await refreshSnapshot()
  if (task.value) syncFormFromTask(task.value, true)
}

function eventTitle(item: WorkEvent) {
  const data = item.data
  const name = typeof data.name === 'string' ? data.name : typeof data.tool_name === 'string' ? data.tool_name : ''
  const state = typeof data.state === 'string' ? stateLabels[data.state as TaskState] ?? data.state : ''
  const labels: Record<string, string> = {
    'task.status_changed': `任务状态${state ? `更新为${state}` : '已更新'}`,
    'draft.updated': `草稿版本更新${typeof data.version === 'number' ? `至 v${data.version}` : ''}`,
    'tool.completed': `工具执行完成${name ? `：${name}` : ''}`,
    'approval.required': '任务进入人工审批等待',
    'order.created': '服务端报告订单创建事件',
    'task.failed': '任务失败事件',
    'span.started': `运行步骤开始${name ? `：${name}` : ''}`,
    'span.completed': `运行步骤完成${name ? `：${name}` : ''}`,
    'span.failed': `运行步骤失败${name ? `：${name}` : ''}`,
    'span.interrupted': '运行步骤等待人工输入',
    'task.order_committed': '服务端确认订单已提交',
    'task.order_commit_blocked': '订单提交被服务端规则阻止',
    'task.resync_required': '事件流要求重新同步任务快照',
  }
  return labels[item.type] ?? `收到服务端事件：${item.type}`
}

function eventDetail(item: WorkEvent) {
  const details: string[] = []
  const duration = item.data.duration_ms
  if (typeof duration === 'number') details.push(`耗时 ${duration} 毫秒`)
  const error = item.data.error
  if (error && typeof error === 'object' && 'code' in error && typeof error.code === 'string') {
    details.push(`错误码 ${error.code}`)
  }
  const usage = item.data.usage
  if (usage && typeof usage === 'object') {
    const inputTokens = 'input_tokens' in usage && typeof usage.input_tokens === 'number'
      ? usage.input_tokens
      : null
    const outputTokens = 'output_tokens' in usage && typeof usage.output_tokens === 'number'
      ? usage.output_tokens
      : null
    const totalTokens = 'total_tokens' in usage && typeof usage.total_tokens === 'number'
      ? usage.total_tokens
      : null
    if (inputTokens !== null && outputTokens !== null) {
      details.push(`Token 输入 ${inputTokens} · 输出 ${outputTokens}${totalTokens === null ? '' : ` · 合计 ${totalTokens}`}`)
    } else if (totalTokens !== null) details.push(`Token ${totalTokens}`)
    else if ('available' in usage && usage.available === false) details.push('Token 用量不可用')
  }
  const output = item.data.output_ref
  if (output && typeof output === 'object' && 'version' in output && typeof output.version === 'number') {
    details.push(`草稿版本 v${output.version}`)
  }
  return details.join(' · ')
}

function stateDescription(state: TaskState) {
  const descriptions: Record<TaskState, string> = {
    QUEUED: '任务已持久化并进入后台队列。', RUNNING: '模型与业务工具正在处理，页面会同步服务端运行事件。',
    WAITING_INPUT: '请补充服务端指出的信息，系统将恢复同一个任务。', WAITING_APPROVAL: '当前版本等待有权限的审批人作出人工决定。',
    SUBMITTING: '审批已通过校验，后台正在执行最终提交并复核额度。', BLOCKED_RULE: '服务端业务规则阻止继续处理，请根据提示修正。',
    FAILED: '任务处理失败；请查看错误详情或刷新后恢复。', COMPLETED: '服务端已确认该任务完成。', REJECTED: '审批人已拒绝此任务。',
  }
  return descriptions[state]
}

async function openTask(taskId: string) {
  await router.push({ name: 'task-workbench', params: { taskId } })
}
</script>

<template>
  <section v-if="pageMode === 'home'" class="page-heading">
    <div>
      <p class="eyebrow">工作空间 / 智能提单</p>
      <h1>开始一笔新订单</h1>
      <p class="page-description">用自然语言描述业务需求；任务、草稿和审批状态均来自服务端。</p>
    </div>
    <div class="page-heading__actions">
      <button class="secondary-button" type="button" :disabled="loading" @click="continueLastTask"><AppIcon name="history" :size="17" />继续上次任务</button>
      <button class="secondary-button" type="button" @click="loadRecentTasks"><AppIcon name="refresh" :size="17" />刷新任务</button>
    </div>
  </section>

  <div v-if="pageMode === 'home'" class="task-home-grid">
    <section class="card task-composer">
      <div class="task-composer__heading">
        <span class="task-composer__icon"><AppIcon name="sparkles" :size="20" /></span>
        <div><h2>描述订单需求</h2><p>提交后会创建真实任务并由后台处理。</p></div>
      </div>
      <div class="task-type-choice" role="group" aria-label="订单类型">
        <button type="button" :class="{ selected: orderType === 'SALES' }" @click="orderType = 'SALES'"><AppIcon name="file" :size="17" />销售订单</button>
        <button type="button" :class="{ selected: orderType === 'PURCHASE' }" @click="orderType = 'PURCHASE'"><AppIcon name="grid" :size="17" />采购订单</button>
      </div>
      <div class="multimodal-tabs" role="tablist" aria-label="输入方式">
        <button type="button" role="tab" :aria-selected="inputMode === 'text'" :class="{ selected: inputMode === 'text' }" @click="inputMode = 'text'">文本描述</button>
        <button type="button" role="tab" :aria-selected="inputMode === 'document'" :class="{ selected: inputMode === 'document' }" @click="inputMode = 'document'">PDF / 图片</button>
        <button type="button" role="tab" :aria-selected="inputMode === 'excel'" :class="{ selected: inputMode === 'excel' }" @click="inputMode = 'excel'">Excel 批量导入</button>
      </div>
      <label for="task-description" class="multimodal-description-label">订单需求描述（必填）</label>
      <textarea id="task-description" v-model="newTaskText" class="task-composer__input" maxlength="12000" placeholder="例如：根据本部门有效合同，为某项目采购 20 吨指定规格的钢材，交期为……" @keydown.ctrl.enter="createTask" @keydown.meta.enter="createTask" />
      <div v-if="inputMode !== 'text'" class="multimodal-panel">
        <div class="multimodal-drop" @dragover.prevent @drop="onFileDrop">
          <strong>{{ inputMode === 'excel' ? '导入订单 Excel' : '上传合同 PDF 或单据图片' }}</strong>
          <span>{{ inputMode === 'excel' ? '仅支持 .xlsx，最大 10 MB' : 'PDF 最大 20 MB；PNG / JPG 最大 10 MB' }}</span>
          <input ref="fileInput" class="sr-only" type="file" :accept="inputMode === 'excel' ? '.xlsx' : '.pdf,.png,.jpg,.jpeg'" @change="onFileChange" />
          <button class="secondary-button" type="button" :disabled="fileBusy" @click="fileInput?.click()">{{ fileBusy ? (inputMode === 'excel' ? '正在校验…' : '正在上传 / 解析…') : '选择文件或拖到此处' }}</button>
          <small v-if="inputMode === 'document'">文件只上传一次；切换面板不会重复上传。字段仍需你确认后才会写入草稿。</small>
          <small v-else>{{ excelStatus }}</small>
        </div>
        <div v-if="inputMode === 'document' && documents.filter(item => pendingDocumentIds.includes(item.artifact.artifact_id)).length" class="multimodal-file-list">
          <button v-for="item in documents.filter(entry => pendingDocumentIds.includes(entry.artifact.artifact_id))" :key="item.artifact.artifact_id" type="button" :class="{ selected: activeArtifactId === item.artifact.artifact_id }" @click="activeArtifactId = item.artifact.artifact_id">
            <strong>{{ item.artifact.original_name }}</strong><span>{{ item.status === 'parsing' ? '正在真实解析…' : item.status === 'parsed' ? `解析完成 · ${item.result?.elapsed_ms ?? 0} ms` : item.message || '已上传' }}</span>
          </button>
        </div>
        <button v-if="inputMode === 'document' && activeDocument?.status === 'failed'" class="text-button" type="button" @click="parseDocument(activeDocument, true)">重试解析（不重新上传）</button>
        <div v-if="inputMode === 'excel' && excelPreview" class="excel-preview-summary">
          <strong>分组校验结果</strong><span>{{ excelPreview.summary.sheet_count }} 个工作表 · {{ excelPreview.summary.detail_rows }} 行明细 · {{ excelPreview.summary.group_count }} 个订单组 · {{ excelPreview.summary.needs_correction_groups }} 组需修正</span>
          <div v-for="group in excelPreview.groups" :key="group.group_id" class="excel-preview-group"><div><b>{{ group.group_id }}</b><span>{{ group.rows.length }} 行 · {{ group.status === 'READY_FOR_REVIEW' ? '待复核' : group.status === 'OVER_QUOTA_REVIEW' ? '超额待复核' : '需修正' }}<template v-if="group.total_amount"> · {{ formatMoney(group.total_amount) }}</template></span><small v-for="issue in group.issues" :key="`${issue.code}:${issue.locator.cell}`">{{ issue.locator.sheet }}!{{ issue.locator.cell ?? '行内字段' }}：{{ issue.message }}</small></div></div>
          <small>创建批次会为结构完整的订单组建立独立任务；仍需业务校验与人工逐笔审批，不会直接创建正式订单。</small>
          <button class="primary-button" type="button" :disabled="creatingBatch || !excelPreview.groups.length" @click="createExcelBatch">{{ creatingBatch ? '正在建立批次…' : '建立批次并逐笔处理' }}</button>
        </div>
        <button v-if="inputMode === 'excel' && excelArtifactId && !excelPreview && !fileBusy" class="text-button" type="button" @click="retryExcelPreview">重试分组校验</button>
      </div>
      <div class="task-composer__footer"><span>Ctrl / ⌘ + Enter 提交 · 身份和部门范围由登录会话确定<template v-if="pendingDocumentIds.length"> · 已附 {{ pendingDocumentIds.length }} 个来源文件</template></span><button class="primary-button" type="button" :disabled="sending || !newTaskText.trim() || inputMode === 'excel'" @click="createTask"><AppIcon v-if="!sending" name="arrow" :size="17" />{{ sending ? '正在创建任务…' : inputMode === 'excel' ? '请先完成分组校验' : '开始解析' }}</button></div>
      <NoticeBanner v-if="errorMessage" tone="danger" :message="errorMessage" />
      <NoticeBanner v-if="noticeMessage" tone="info" :message="noticeMessage" />
    </section>
    <aside class="card recent-tasks">
      <div class="section-title-row"><div><h2>最近任务</h2><p>由当前身份可访问的任务</p></div><button class="icon-button" type="button" aria-label="刷新最近任务" @click="loadRecentTasks"><AppIcon name="refresh" :size="17" /></button></div>
      <div v-if="loading && !taskList.length" class="task-empty">正在读取任务列表…</div>
      <div v-else-if="!taskList.length" class="task-empty"><AppIcon name="history" :size="24" /><strong>暂无任务记录</strong><span>提交一段真实需求后，任务会显示在这里。</span></div>
      <button v-for="item in taskList" :key="item.task_id" class="recent-task-row" type="button" @click="openTask(item.task_id)">
        <span class="recent-task-row__main"><strong>{{ item.order_type === 'SALES' ? '销售订单' : '采购订单' }} · {{ item.task_id.slice(0, 8) }}</strong><small>{{ formatTime(item.updated_at) }}<template v-if="item.draft_version"> · 草稿 v{{ item.draft_version }}</template></small></span>
        <StatusBadge :label="stateLabels[item.state]" :tone="item.state === 'COMPLETED' ? 'success' : ['FAILED','BLOCKED_RULE','REJECTED'].includes(item.state) ? 'danger' : ['WAITING_INPUT','WAITING_APPROVAL'].includes(item.state) ? 'warning' : 'info'" />
      </button>
      <div class="recent-tasks__help"><strong>使用帮助</strong><p>PDF / 图片解析会保留页码来源，字段需逐项确认后应用；Excel 只读校验分组和单元格。草稿仍需业务校验与人工审批。</p></div>
    </aside>
  </div>

  <template v-else>
    <div v-if="loading && !task" class="task-load-state">正在读取任务快照…</div>
    <NoticeBanner v-if="errorMessage" tone="danger" :message="errorMessage" />
    <template v-if="task">
      <section class="task-progress-bar">
        <div><p class="eyebrow">{{ task.order_type === 'SALES' ? '销售订单' : '采购订单' }} / 任务 {{ task.task_id.slice(0, 8) }}</p><h1>{{ task.draft ? `订单草稿 · 第 ${task.draft.version} 版` : '订单处理任务' }}</h1><p>{{ stateDescription(task.state) }}</p></div>
        <div class="task-progress-bar__right"><StatusBadge :label="stateLabels[task.state]" :tone="stateTone" /><button class="secondary-button" type="button" @click="refreshSnapshot()"><AppIcon name="refresh" :size="16" />刷新</button><RouterLink class="secondary-button" :to="{ name: 'workbench' }"><AppIcon name="plus" :size="16" />新任务</RouterLink></div>
      </section>
      <NoticeBanner v-if="task.last_error" tone="danger" :message="typeof task.last_error.message === 'string' ? task.last_error.message : typeof task.last_error.code === 'string' ? `处理失败：${task.last_error.code}` : '任务处理失败，请查看运行轨迹并刷新。'" />
      <section v-if="loadingFailureSpan || focusedFailureSpan || focusedFailureError" class="focused-failure-span" aria-live="polite">
        <strong>{{ loadingFailureSpan ? '正在读取真实失败轨迹…' : focusedFailureSpan ? `失败位置：${focusedFailureSpan.name}` : '失败轨迹不可用' }}</strong>
        <p v-if="focusedFailureError">{{ focusedFailureError }}</p>
        <template v-if="focusedFailureSpan"><span>{{ focusedFailureSpan.kind }} · {{ focusedFailureSpan.status }} · 第 {{ focusedFailureSpan.attempt }} 次 · {{ focusedFailureSpan.duration_ms === null ? '耗时不可用' : `${focusedFailureSpan.duration_ms} ms` }}</span><span>错误码 {{ focusedFailureSpan.error.code ?? '未知' }} · {{ focusedFailureSpan.error.retryable ? '服务端允许安全重试' : '不可重试' }}</span><code>Span {{ focusedFailureSpan.span_id }} · Trace {{ focusedFailureSpan.trace_id }}</code></template>
      </section>
      <NoticeBanner v-if="task.state === 'WAITING_INPUT' && missingLabels.length" tone="warning" :message="`还需要补充：${missingLabels.join('、')}`" />
      <NoticeBanner v-if="task.state === 'BLOCKED_RULE' && draftIssues.length" tone="danger" :message="draftIssues.map(issue => issue.message).join('；')" />
      <NoticeBanner v-if="keepLocalDraft" tone="warning" message="服务端草稿版本已变化。本页保留了你尚未保存的修改，没有覆盖它们；请检查最新错误和当前等待状态。" />
      <NoticeBanner v-if="noticeMessage" tone="info" :message="noticeMessage" />

      <div class="task-workbench-grid">
        <section class="card task-conversation">
          <div class="task-section-heading"><div><span class="section-kicker">任务对话</span><h2>业务需求与运行状态</h2></div><span class="live-indicator"><i></i>同步服务端</span></div>
          <div class="conversation-scroll">
            <article v-for="message in task.inputs" :key="message.command_id" class="conversation-message conversation-message--user"><span class="conversation-message__avatar">{{ auth.identity?.login.slice(0, 1).toUpperCase() }}</span><div><div class="conversation-message__meta"><strong>业务需求</strong><time>{{ formatTime(message.created_at) }}</time></div><p>{{ message.text }}</p></div></article>
            <article v-if="task.active_wait?.reason" class="conversation-message conversation-message--system"><span class="conversation-message__avatar"><AppIcon name="sparkles" :size="16" /></span><div><div class="conversation-message__meta"><strong>{{ task.active_wait.kind === 'APPROVAL' ? '审批等待' : '需要补充信息' }}</strong><span>服务端任务状态</span></div><p>{{ task.active_wait.reason }}</p><ul v-if="missingLabels.length" class="missing-field-list"><li v-for="label in missingLabels" :key="label">{{ label }}</li></ul></div></article>
            <article v-if="task.latest_review" class="conversation-message conversation-message--system"><span class="conversation-message__avatar"><AppIcon name="shield" :size="16" /></span><div><div class="conversation-message__meta"><strong>人工审批记录</strong><time>{{ formatTime(task.latest_review.created_at) }}</time></div><p>{{ task.latest_review.decision === 'APPROVE' ? '已批准' : task.latest_review.decision === 'RETURN' ? '已退回' : '已拒绝' }}<template v-if="task.latest_review.invalidated"> · 该决定已因草稿变更失效</template><template v-if="task.latest_review.comment">：{{ task.latest_review.comment }}</template></p><small>审批绑定草稿 v{{ task.latest_review.version }} · 当前 v{{ task.draft?.version ?? '—' }}</small></div></article>
            <div v-if="task.order_ref" class="order-created-result"><AppIcon name="check" :size="18" /><div><strong>服务端已确认订单创建</strong><span>{{ task.order_ref.order_number }} · {{ formatMoney(task.order_ref.total_amount) }} {{ task.order_ref.currency }}</span></div><button class="secondary-button" type="button" @click="openCreatedOrder">在订单中心查看</button></div>
            <div v-if="activity.length" class="activity-section"><h3>任务运行轨迹与事件</h3><ol class="activity-list"><li v-for="item in activity" :key="`${item.seq}-${item.type}`"><span class="activity-list__dot"></span><div><strong>{{ eventTitle(item) }}</strong><small>{{ formatTime(item.emitted_at) }}<template v-if="item.seq"> · 事件 {{ item.seq }}</template></small><small v-if="eventDetail(item)">{{ eventDetail(item) }}</small></div></li></ol></div>
            <div v-if="!task.inputs.length && !task.order_ref" class="conversation-empty">等待服务端写入任务内容和运行状态…</div>
          </div>
          <form class="reply-composer" @submit.prevent="sendReply">
            <label class="sr-only" for="task-reply">补充订单信息</label>
            <textarea id="task-reply" v-model="replyText" :disabled="!canReply || sending" maxlength="12000" :placeholder="canReply ? '补充缺失字段、澄清业务需求…' : task.state === 'RUNNING' || task.state === 'QUEUED' ? '任务处理中，等待服务端开放下一个输入步骤…' : '当前没有等待补充信息的步骤'" />
            <div><span>只有服务端处于等待补充状态时才能继续原任务。</span><button class="primary-button" type="submit" :disabled="!canReply || sending || !replyText.trim()">{{ sending ? '正在提交…' : '继续任务' }}<AppIcon v-if="!sending" name="arrow" :size="16" /></button></div>
          </form>
        </section>

        <section class="task-draft-column">
          <article class="card evidence-card">
            <div class="task-section-heading"><div><span class="section-kicker">来源可追溯</span><h2>文件证据与字段确认</h2></div><StatusBadge v-if="activeDocument?.result" :label="activeDocument.result.state === 'COMPLETE' ? '解析完成' : '部分解析'" :tone="activeDocument.result.state === 'COMPLETE' ? 'success' : 'warning'" /></div>
            <div class="evidence-toolbar">
              <div class="multimodal-file-list" v-if="taskArtifacts.length">
                <button v-for="item in taskArtifacts" :key="item.artifact_id" type="button" :class="{ selected: activeArtifactId === item.artifact_id }" @click="inspectArtifact(item)"><strong>{{ item.original_name }}</strong><span>{{ (item.byte_size / 1024).toFixed(0) }} KB · {{ item.media_type }}</span></button>
              </div>
              <label v-if="canUploadTaskFile" class="evidence-upload-button">
                <input ref="fileInput" class="sr-only" type="file" accept=".pdf,.png,.jpg,.jpeg" :disabled="fileBusy" @change="onFileChange" />
                <span>{{ fileBusy ? '正在上传 / 解析…' : '＋ 添加 PDF / 图片' }}</span>
              </label>
              <span v-else-if="!taskArtifacts.length" class="evidence-empty">任务当前没有已授权附件。</span>
            </div>
            <NoticeBanner v-if="activeDocument?.message" tone="danger" :message="activeDocument.message" />
            <div v-if="activeDocument" class="evidence-preview-layout">
              <div class="evidence-source-preview">
                <div class="evidence-preview-title"><strong>{{ activeDocument.artifact.original_name }}</strong><span>{{ activeDocument.result ? `第 ${activeEvidencePage} 页` : '原始文件' }}</span></div>
                <iframe v-if="activeDocument.previewUrl && activeDocument.artifact.media_type === 'application/pdf'" :src="`${activeDocument.previewUrl}#page=${activeEvidencePage}`" :title="`原始文件 ${activeDocument.artifact.original_name}`" />
                <img v-else-if="activeDocument.previewUrl" :src="activeDocument.previewUrl" :alt="`原始文件 ${activeDocument.artifact.original_name}`" />
                <button v-else class="secondary-button" type="button" :disabled="activeDocument.status === 'parsing'" @click="inspectArtifact(activeDocument.artifact)">{{ activeDocument.status === 'parsing' ? '正在读取…' : '打开原始文件并解析' }}</button>
              </div>
              <div class="evidence-fields">
                <template v-if="activeDocument.result">
                  <label v-for="(field, index) in activeDocument.result.fields" :key="evidenceFieldId(field, index)" class="evidence-field" :class="{ 'evidence-field--conflict': field.status === 'CONFLICT' }">
                    <input v-if="fieldPathForEvidence(field)" type="radio" :name="`${activeArtifactId}-${field.field}`" :value="evidenceFieldId(field, index)" :disabled="!canEditDraft || isDirty || applyingEvidence" v-model="evidenceSelections[field.field]" />
                    <span class="evidence-field__body"><strong>{{ fieldLabelForEvidence(field.field) }}：{{ field.value }}<small v-if="field.unit"> {{ field.unit }}</small></strong><small>第 {{ field.page_number }} 页 · {{ field.status === 'CONFLICT' ? '存在冲突，需选择一项' : '待人工确认' }}</small><code>{{ field.raw_text }}</code></span>
                    <span v-if="field.status === 'CONFLICT'" class="evidence-conflict-tag">冲突候选</span>
                    <span v-if="!fieldPathForEvidence(field)" class="evidence-field__manual">需人工匹配</span>
                  </label>
                  <div v-if="activeDocument.result.missing_fields.length" class="evidence-missing"><strong>未识别字段</strong><span>{{ activeDocument.result.missing_fields.map(fieldLabelForEvidence).join('、') }}</span></div>
                  <p v-if="!activeDocument.result.fields.length" class="evidence-empty">没有识别出可确认字段；原文件仍保留，可手工编辑草稿。</p>
                </template>
                <div v-else class="evidence-empty"><span>{{ activeDocument.status === 'parsing' ? '正在解析文件，请稍候…' : '选择附件以查看解析结果和页内来源。' }}</span><button v-if="activeDocument.status === 'failed'" class="text-button" type="button" @click="parseDocument(activeDocument, true)">重试解析</button></div>
              </div>
            </div>
            <div v-if="activeDocument?.result" class="evidence-apply-row">
              <span>已选择 {{ selectedEvidenceCount }} 个字段 · 确认后保存并重新校验，字段来源写入审计记录</span>
              <button class="primary-button" type="button" :disabled="!canEditDraft || isDirty || !selectedEvidenceCount || applyingEvidence || saving" @click="applySelectedEvidence">{{ applyingEvidence ? '正在应用并校验…' : isDirty ? '请先保存当前草稿修改' : '确认并应用到草稿' }}</button>
            </div>
          </article>
          <article class="card draft-card">
            <div class="task-section-heading"><div><span class="section-kicker">订单预览</span><h2>草稿字段</h2></div><StatusBadge v-if="task.draft" :label="task.draft.validated ? '结构校验通过' : '需要补齐'" :tone="task.draft.validated ? 'success' : 'warning'" /></div>
            <template v-if="draftForm && task.draft">
              <div class="draft-reference-grid">
                <label><span>合同编号（引用）</span><code :title="draftForm.contract_id ?? '未识别'">{{ draftForm.contract_id ? draftForm.contract_id.slice(0, 12) + '…' : '待确认' }}</code></label>
                <label><span>客户引用</span><code :title="draftForm.customer_id ?? '未识别'">{{ draftForm.customer_id ? draftForm.customer_id.slice(0, 12) + '…' : '待确认' }}</code></label>
                <label><span>项目引用</span><code :title="draftForm.project_id ?? '未识别'">{{ draftForm.project_id ? draftForm.project_id.slice(0, 12) + '…' : '待确认' }}</code></label>
                <label><span>订单方向 / 币种</span><strong>{{ draftForm.order_type === 'PURCHASE' ? '采购' : '销售' }} · {{ draftForm.currency }}</strong></label>
                <label :class="{ 'field-missing': missingLabels.includes('交期') }"><span>交期</span><input v-model="draftForm.delivery_date" type="date" :disabled="!canEditDraft" /></label>
                <label :class="{ 'field-missing': missingLabels.includes('收货地址') }"><span>收货地址</span><input v-model="draftForm.address" :disabled="!canEditDraft" placeholder="待补充" /></label>
                <label :class="{ 'field-missing': missingLabels.includes('联系人') }"><span>联系人</span><input v-model="draftForm.contact" :disabled="!canEditDraft" placeholder="待补充" /></label>
              </div>
              <div v-if="draftIssues.length" class="draft-issue-list"><p v-for="issue in draftIssues" :key="`${issue.code}:${issue.field}`"><strong>{{ fieldLabel(issue.field) }}</strong>{{ issue.message }}</p></div>
              <div class="draft-items-heading"><div><h3>产品明细</h3><p>编辑后需保存并由服务端重新校验</p></div><button v-if="canEditDraft" class="text-button" type="button" @click="addItem">＋ 添加一行</button></div>
              <div class="draft-items-wrap">
                <table class="draft-items-table"><thead><tr><th>物料 / 规格</th><th>数量 / 单位</th><th>单价</th><th>金额</th><th v-if="canEditDraft"></th></tr></thead>
                  <tbody>
                    <template v-for="(item, index) in draftForm.items" :key="`${index}:${item.material_id ?? item.raw_name}`">
                      <tr>
                        <td><input v-model="item.raw_name" :disabled="!canEditDraft" aria-label="物料名称" placeholder="物料名称" /><input v-model="item.specification" :disabled="!canEditDraft" aria-label="物料规格" placeholder="规格" class="draft-sub-input" /><button v-if="canEditDraft" class="material-match-trigger" type="button" :disabled="isDirty || saving || matchingItems.has(index)" @click="findMaterialMatches(index)">{{ matchingItems.has(index) ? '查询中…' : '匹配标准物料' }}</button><small v-if="isDirty" class="material-match-hint">先保存当前编辑，再查询候选</small></td>
                        <td class="quantity-cell"><input v-model="item.quantity" :disabled="!canEditDraft" inputmode="decimal" aria-label="数量" placeholder="数量" /><input v-model="item.unit" :disabled="!canEditDraft" aria-label="单位" placeholder="单位" /></td>
                        <td><input v-model="item.unit_price" :disabled="!canEditDraft" inputmode="decimal" aria-label="单价" placeholder="单价" /></td>
                        <td class="draft-line-amount">{{ lineAmount(item) ? formatMoney(lineAmount(item)) : '待核算' }}</td>
                        <td v-if="canEditDraft"><button class="remove-item-button" type="button" :aria-label="`删除第 ${index + 1} 行`" @click="removeItem(index)">×</button></td>
                      </tr>
                      <tr v-if="materialMatches[index]" :key="`matches-${index}-${task.draft.version}`" class="material-match-result-row">
                        <td :colspan="canEditDraft ? 5 : 4">
                          <div class="material-match-result">
                            <div class="material-match-result__heading"><strong>{{ item.raw_name || '物料' }} → 标准物料候选</strong><span>{{ materialMatches[index].data.candidates.length ? `找到 ${materialMatches[index].data.candidates.length} 个候选` : '没有匹配候选' }}</span></div>
                            <NoticeBanner v-for="issue in materialMatches[index].issues" :key="issue.code" tone="warning" :message="issue.message" />
                            <p v-if="!materialMatches[index].data.candidates.length" class="material-match-empty">请补充或修正名称、规格和单位后再查询。</p>
                            <div v-for="candidate in materialMatches[index].data.candidates" :key="candidate.material_id" class="material-match-candidate" :class="{ 'material-match-candidate--selected': item.material_id === candidate.material_id }">
                              <div class="material-match-candidate__detail"><strong>{{ candidate.code }} · {{ candidate.name }}</strong><span>{{ candidate.specification || '规格待确认' }} · {{ candidate.unit || '单位待确认' }}</span><small>{{ candidate.reason }}<template v-if="candidate.requires_confirmation"> · 仍需人工确认</template></small></div>
                              <div v-if="canEditDraft" class="material-match-candidate__actions"><button class="material-match-accept" type="button" :disabled="isDirty || saving || item.material_id === candidate.material_id" @click="acceptMaterialCandidate(index, candidate)">{{ saving && item.material_id === candidate.material_id ? '正在应用…' : item.material_id === candidate.material_id ? '已采纳' : '采纳并重新校验' }}</button><button class="material-match-reject" type="button" :disabled="isDirty || saving || materialDecisions.has(`${index}:${candidate.material_id}`)" @click="rejectMaterialCandidate(index, candidate)">{{ materialDecisions.has(`${index}:${candidate.material_id}`) ? '已记录拒绝' : '拒绝候选' }}</button></div>
                            </div>
                          </div>
                        </td>
                      </tr>
                    </template>
                    <tr v-if="!draftForm.items.length"><td class="draft-table-empty" :colspan="canEditDraft ? 5 : 4">当前草稿尚无产品行；缺失字段以服务端校验为准。</td></tr>
                  </tbody>
                </table>
              </div>
              <div class="draft-total-row"><span>草稿总金额 <small>{{ isDirty ? '本地精确试算 · 尚未通过服务端重校' : '服务端金额' }}</small></span><strong>{{ formatMoney(totalAmount) }}<small> {{ draftForm.currency }}</small></strong></div>
              <div class="draft-version-row"><span>草稿 v{{ task.draft.version }} · {{ task.draft.content_hash.slice(0, 12) }}…</span><span>{{ isDirty ? '有未保存修改' : '已与服务端同步' }}</span></div>
              <button v-if="canEditDraft" class="secondary-button draft-save-button" type="button" :disabled="!isDirty || saving" @click="saveDraft"><AppIcon name="save" :size="16" />{{ saving ? '正在保存并校验…' : isDirty ? '保存并重新校验' : '草稿已保存' }}</button>
              <button v-if="keepLocalDraft" class="text-button draft-reload-button" type="button" @click="reloadServerDraft">放弃本地修改并载入最新版本</button>
            </template>
            <div v-else class="draft-empty"><AppIcon name="file" :size="28" /><strong>{{ task.draft ? '当前任务没有可编辑的候选草稿' : '草稿尚未生成' }}</strong><p>{{ task.state === 'RUNNING' || task.state === 'QUEUED' ? '等待模型与后端工具产出真实草稿。' : '补充需求后，后端将返回可核验字段。' }}</p></div>
          </article>

          <article v-if="quota" class="card quota-card">
            <div class="task-section-heading"><div><span class="section-kicker">服务端实时数据</span><h2>合同额度预览</h2></div><AppIcon name="chart" :size="18" /></div>
            <template v-if="quota.balance_available">
              <div class="quota-progress-label"><span>{{ isDirty ? '修改后预计剩余额度' : '剩余额度' }}</span><strong>{{ formatMoney(quotaRemaining) }} {{ task.draft?.candidate?.currency ?? 'CNY' }}</strong></div>
              <div class="quota-progress-track"><span :class="{ 'quota-progress-track--over': shouldBlockByQuota }" :style="{ width: `${quotaPercent}%` }"></span></div>
              <div class="quota-details"><span>合同总额 <strong>{{ formatMoney(quota.contract_amount) }}</strong></span><span>已占用 <strong>{{ formatMoney(quota.occupied_amount) }}</strong></span><span>本单金额 <strong>{{ formatMoney(quotaRequested) }}</strong></span></div>
              <NoticeBanner v-if="shouldBlockByQuota" tone="danger" :message="`当前金额超出合同额度 ${formatMoney(quotaExcess)}，审批和提交将被服务端阻止。`" />
              <p v-else-if="!quotaRequested" class="quota-footnote">产品行数量或单价未齐全；当前只显示服务端已读取的合同余额。</p>
            </template>
            <NoticeBanner v-else tone="danger" message="服务端未能读取合同余额，额度不可用；不得据此批准或提交订单。" />
          </article>
          <article v-else class="quota-unavailable"><AppIcon name="shield" :size="17" /><span>尚未从服务端取得合同及额度；页面不会用示例数字代替。</span></article>

          <article v-if="task.state === 'WAITING_APPROVAL'" class="card approval-card">
            <div class="task-section-heading"><div><span class="section-kicker">人工决策</span><h2>待审批版本</h2></div><StatusBadge label="等待审批" tone="warning" /></div>
            <p>批准仅针对当前草稿版本和内容摘要。HTTP 202 表示决定已受理，不代表订单已创建。</p>
            <div class="approval-binding">版本 v{{ task.active_wait?.draft_version ?? task.draft?.version ?? '—' }} · 摘要 {{ task.active_wait?.content_hash?.slice(0, 12) ?? '—' }}…</div>
            <div v-if="canApprove && !shouldBlockByQuota && !quotaUnavailable" class="approval-actions"><button class="primary-button" type="button" :disabled="reviewing || !task.draft?.validated" @click="decide('APPROVE')"><AppIcon name="check" :size="16" />批准并继续</button><button class="secondary-button" type="button" :disabled="reviewing" @click="openReviewDialog('RETURN')">退回修改</button><button class="danger-button" type="button" :disabled="reviewing" @click="openReviewDialog('REJECT')">拒绝任务</button></div>
            <NoticeBanner v-else-if="quotaUnavailable" tone="danger" message="服务端合同额度不可用，审批操作已禁用；请先核对合同和额度账本。" />
            <NoticeBanner v-else-if="shouldBlockByQuota" tone="danger" message="额度超限，批准操作已禁用；备注不能豁免服务端额度规则。" />
            <NoticeBanner v-else tone="warning" message="当前身份没有审批角色或草稿尚未通过服务端校验。" />
          </article>
          <article v-else-if="task.latest_review?.invalidated" class="card approval-card"><NoticeBanner tone="warning" message="此前审批因草稿修改已经失效，当前版本需要重新审批。" /></article>
        </section>
      </div>
    </template>
    <div v-if="reviewDialog" class="review-modal-backdrop" role="presentation" @click.self="reviewDialog = null">
      <section class="review-modal" role="dialog" aria-modal="true" :aria-labelledby="'review-title'">
        <h2 id="review-title">{{ reviewDialog === 'RETURN' ? '退回补充说明' : '拒绝任务' }}</h2>
        <p>决定会绑定当前草稿版本 v{{ task?.draft?.version }}。</p>
        <label for="review-reason">原因（必填）</label><textarea id="review-reason" v-model="reviewReason" maxlength="1000" placeholder="请说明退回或拒绝原因" />
        <div class="review-modal__actions"><button class="secondary-button" type="button" @click="reviewDialog = null">取消</button><button class="danger-button" type="button" :disabled="reviewing || !reviewReason.trim()" @click="decide(reviewDialog!, reviewReason.trim())">{{ reviewing ? '提交中…' : '确认提交决定' }}</button></div>
      </section>
    </div>
  </template>
</template>
