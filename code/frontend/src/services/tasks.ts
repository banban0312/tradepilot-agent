import { useAuthStore } from '../stores/auth'

export type OrderType = 'SALES' | 'PURCHASE'
export type TaskState =
  | 'QUEUED'
  | 'RUNNING'
  | 'WAITING_INPUT'
  | 'WAITING_APPROVAL'
  | 'SUBMITTING'
  | 'BLOCKED_RULE'
  | 'FAILED'
  | 'COMPLETED'
  | 'REJECTED'

export type DraftItem = {
  material_id: string | null
  raw_name: string | null
  specification: string | null
  quantity: string | null
  unit: string | null
  unit_price: string | null
}

export type MaterialCandidate = {
  material_id: string
  code: string
  name: string
  specification: string | null
  unit: string | null
  reason: string
  requires_confirmation: boolean
}

export type MaterialMatchResult = {
  data: { candidates: MaterialCandidate[] }
  issues: Array<{ code: string; message: string }>
  refs: string[]
}

export type DraftCandidate = {
  order_type: OrderType | null
  contract_id: string | null
  customer_id: string | null
  project_id: string | null
  currency: string
  delivery_date: string | null
  address: string | null
  contact: string | null
  business_fields: Record<string, unknown>
  items: DraftItem[]
  evidence: Array<Record<string, unknown>>
}
export type FileFieldConfirmation = {
  field_path: string
  artifact_id: string
  page_number: number
  locator: { type: 'text_range' | 'bbox'; [key: string]: number | string }
  raw_value: string
}

export type DraftIssue = { code: string; field: string; message: string }
export type QuotaPreview = {
  contract_amount: string
  occupied_amount: string | null
  remaining_amount: string | null
  requested_amount: string | null
  excess_amount: string | null
  balance_available: boolean
}

export type TaskSnapshot = {
  task_id: string
  session_id: string
  order_type: OrderType
  state: TaskState
  projection_version: number
  current_step: string | null
  inputs: Array<{ command_id: string; role: 'user'; text: string; created_at: string }>
  draft: null | {
    draft_id: string
    version: number
    content_hash: string
    total_amount: string | null
    validated: boolean
    issues: DraftIssue[]
    candidate: DraftCandidate | null
  }
  quota_preview: QuotaPreview | null
  active_wait: null | {
    kind: 'INPUT' | 'APPROVAL'
    checkpoint_id: string
    interrupt_id: string
    draft_version: number | null
    content_hash: string | null
    missing_fields: string[]
    reason: string | null
  }
  latest_review: null | {
    decision: string
    actor_id: string
    department_id: string
    draft_id: string
    version: number
    content_hash: string
    comment: string | null
    invalidated: boolean
    created_at: string
  }
  order_ref: null | {
    order_id: string
    order_number: string
    state: string
    total_amount: string
    currency: string
    approval_id: string
    approved_version: number
    approved_content_hash: string
  }
  last_error: null | Record<string, unknown>
  available_actions: string[]
  created_at: string
  updated_at: string
}

export type FailedTaskSpan = {
  span_id: string
  trace_id: string
  task_id: string
  parent_span_id: string | null
  name: string
  kind: string
  attempt: number
  execution_mode: string
  protocol: string | null
  status: 'ERROR' | 'INTERRUPTED'
  start_at: string
  end_at: string | null
  duration_ms: number | null
  error: { code: string | null; retryable: boolean }
}

export type TaskSummary = {
  task_id: string
  session_id: string
  order_type: OrderType
  state: TaskState
  projection_version: number
  draft_version: number | null
  input_type: string
  created_at: string
  updated_at: string
  status_url: string
}

export type TaskBatchTree = {
  batch_id: string
  created_at: string
  state: string
  groups: Array<{
    group_id: string
    sheet_name: string
    source_rows: number[]
    preview_status: string
    stop_result: string | null
    task_id: string | null
    task_state: string | null
  }>
}
export type TaskList = { items: TaskSummary[]; next_cursor: string | null; batches: TaskBatchTree[] }
export type TaskHistoryFilters = {
  state?: TaskState | ''
  input_type?: 'TEXT' | 'TEXT_AND_FILES' | 'EXCEL_GROUP' | ''
  order_type?: OrderType | ''
  q?: string
  created_after?: string
  created_before?: string
}
export type TaskTraceSpan = {
  span_id: string
  trace_id: string
  parent_span_id: string | null
  kind: string
  name: string
  attempt: number
  execution_mode: string
  protocol: string | null
  status: string
  start_at: string
  end_at: string | null
  duration_ms: number | null
  input: Record<string, unknown> | null
  output: Record<string, unknown> | null
  usage: { input_tokens: number; output_tokens: number; total_tokens: number } | null
  usage_status: 'available' | 'unavailable'
  error: { code: string | null; retryable: boolean }
  event_seq: number | null
}
export type TaskTrace = {
  task_id: string
  state: TaskState
  spans: TaskTraceSpan[]
  next_cursor: string | null
  metrics: {
    model_calls: number
    usage_available_calls: number
    usage_coverage: number | null
    usage_status: 'available' | 'unavailable'
    tokens: { input_tokens: number; output_tokens: number; total_tokens: number } | null
    model_duration_ms: number
    tool_duration_ms: number
    manual_waits: Array<{ state: string | null; start_at: string; end_at: string | null; duration_ms: number; active: boolean }>
  }
  approvals: Array<{ approval_id: string; actor_id: string; decision: string; version: number; content_hash: string; invalidated: boolean; created_at: string }>
  checkpoints: Array<{ checkpoint_id: string; interrupt_id: string; kind: string; draft_version: number | null; active: boolean; created_at: string }>
  audit_events: Array<{ action: string; result: string; actor_id: string | null; created_at: string; object_ref: Record<string, unknown> }>
  audit_events_truncated: boolean
  order: null | { order_id: string; order_number: string; state: string }
  events: Array<{ seq: number; type: string; span_id: string | null; payload: Record<string, unknown>; emitted_at: string }>
  events_truncated: boolean
}
type ApiEnvelope<T> = { data: T; error?: { code?: string; message?: string }; detail?: { code?: string; message?: string } }

function requestId() {
  return crypto.randomUUID()
}

function apiError(body: ApiEnvelope<unknown>, status: number) {
  const error = body.error ?? body.detail
  const message = error?.message ?? `请求失败（HTTP ${status}），请刷新后重试。`
  const exception = new Error(message) as Error & { code?: string; status?: number }
  exception.code = error?.code ?? 'REQUEST_FAILED'
  exception.status = status
  return exception
}

async function request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
  const auth = useAuthStore()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined) headers.set('Content-Type', 'application/json')
  if (init.method && init.method !== 'GET') {
    const csrf = auth.identity?.csrf_token
    if (!csrf) throw new Error('当前登录状态缺少安全校验信息，请刷新页面后重试。')
    headers.set('X-CSRF-Token', csrf)
    headers.set('X-Request-ID', requestId())
    headers.set('Idempotency-Key', idempotencyKey ?? requestId())
  }

  let response: Response
  try {
    response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  } catch {
    throw new Error('暂时无法连接任务服务，请检查 Docker 服务和网络。')
  }
  const body = (await response.json()) as ApiEnvelope<T>
  if (!response.ok) throw apiError(body, response.status)
  return body.data
}

export const tasksApi = {
  list: (filters: TaskHistoryFilters = {}, cursor?: string | null, limit = 20) => {
    const params = new URLSearchParams({ limit: String(limit) })
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value)
    }
    if (cursor) params.set('cursor', cursor)
    return request<TaskList>(`/api/v1/tasks?${params.toString()}`)
  },
  get: (taskId: string) => request<TaskSnapshot>(`/api/v1/tasks/${encodeURIComponent(taskId)}`),
  getTrace: (taskId: string, cursor?: string | null, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (cursor) params.set('cursor', cursor)
    return request<TaskTrace>(`/api/v1/tasks/${encodeURIComponent(taskId)}/trace?${params.toString()}`)
  },
  getFailedSpan: (taskId: string, spanId: string) => request<FailedTaskSpan>(`/api/v1/tasks/${encodeURIComponent(taskId)}/trace/${encodeURIComponent(spanId)}`),
  retry: (task: TaskSnapshot) => {
    const spanId = task.last_error?.failed_span_id
    if (task.state !== 'FAILED' || !task.available_actions.includes('RETRY') || typeof spanId !== 'string') {
      throw new Error('该任务当前没有可安全恢复的失败，请刷新后核对状态。')
    }
    const idempotencyKey = `retry-${task.task_id}-${task.projection_version}-${spanId}`
    return request<Record<string, unknown>>(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/retry`, {
      method: 'POST',
      body: JSON.stringify({ failed_span_id: spanId, expected_projection_version: task.projection_version }),
    }, idempotencyKey)
  },
  create: (orderType: OrderType, text: string, artifactIds: string[] = []) =>
    request<TaskSummary>('/api/v1/tasks', {
      method: 'POST',
      body: JSON.stringify({ order_type: orderType, input: { text, artifact_ids: artifactIds } }),
    }),
  supplement: (task: TaskSnapshot, text: string) => {
    if (!task.active_wait) throw new Error('任务当前没有可继续的补充信息等待点，请刷新任务。')
    return request<Record<string, unknown>>(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/inputs`, {
      method: 'POST',
      body: JSON.stringify({
        interrupt_id: task.active_wait.interrupt_id,
        checkpoint_id: task.active_wait.checkpoint_id,
        expected_draft_version: task.draft?.version ?? null,
        text,
      }),
    })
  },
  patchDraft: (
    task: TaskSnapshot,
    patch: Partial<DraftCandidate>,
    fileConfirmations: FileFieldConfirmation[] = [],
  ) => {
    const wait = task.active_wait
    const editableWait = wait?.kind === 'INPUT'
      || (task.state === 'QUEUED' && wait?.kind === 'APPROVAL')
    if (!wait || !editableWait || !task.available_actions.includes('EDIT_DRAFT') || !task.draft) {
      throw new Error('当前任务不能编辑草稿，请等待补充信息步骤后重试。')
    }
    return request<Record<string, unknown>>(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/draft`, {
      method: 'PATCH',
      body: JSON.stringify({
        expected_version: task.draft.version,
        interrupt_id: wait.interrupt_id,
        checkpoint_id: wait.checkpoint_id,
        patch,
        file_confirmations: fileConfirmations,
      }),
    })
  },
  matchMaterials: (task: TaskSnapshot, itemIndex: number) => {
    if (!task.draft || !task.available_actions.includes('EDIT_DRAFT')) {
      throw new Error('当前任务不能确认物料，请刷新后重试。')
    }
    return request<MaterialMatchResult>(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/material-matches`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: task.draft.version, item_index: itemIndex }),
    })
  },
  rejectMaterial: (task: TaskSnapshot, itemIndex: number, candidateMaterialId: string) => {
    if (!task.draft || !task.available_actions.includes('EDIT_DRAFT')) {
      throw new Error('当前任务不能记录物料决定，请刷新后重试。')
    }
    return request<Record<string, unknown>>(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/material-decisions`, {
      method: 'POST',
      body: JSON.stringify({
        expected_version: task.draft.version,
        item_index: itemIndex,
        candidate_material_id: candidateMaterialId,
        decision: 'REJECT',
      }),
    })
  },
  review: (task: TaskSnapshot, decision: 'APPROVE' | 'RETURN' | 'REJECT', reason?: string) => {
    if (!task.active_wait || task.active_wait.kind !== 'APPROVAL' || !task.draft) {
      throw new Error('当前任务已不在审批等待状态，请刷新后查看最新版本。')
    }
    return request<Record<string, unknown>>(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/review`, {
      method: 'POST',
      body: JSON.stringify({
        decision,
        draft_version: task.draft.version,
        content_hash: task.draft.content_hash,
        checkpoint_id: task.active_wait.checkpoint_id,
        interrupt_id: task.active_wait.interrupt_id,
        ...(reason ? { reason } : {}),
      }),
    })
  },
}

export const TASK_EVENT_TYPES = [
  'task.status_changed',
  'draft.updated',
  'tool.completed',
  'approval.required',
  'order.created',
  'task.failed',
  'span.started',
  'span.completed',
  'span.failed',
  'span.interrupted',
  'task.order_committed',
  'task.order_commit_blocked',
  'task.resync_required',
] as const
