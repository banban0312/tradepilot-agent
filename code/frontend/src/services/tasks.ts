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

export type TaskList = { items: TaskSummary[]; next_cursor: string | null }
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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined) headers.set('Content-Type', 'application/json')
  if (init.method && init.method !== 'GET') {
    const csrf = auth.identity?.csrf_token
    if (!csrf) throw new Error('当前登录状态缺少安全校验信息，请刷新页面后重试。')
    headers.set('X-CSRF-Token', csrf)
    headers.set('X-Request-ID', requestId())
    headers.set('Idempotency-Key', requestId())
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
  list: () => request<TaskList>('/api/v1/tasks?limit=20'),
  get: (taskId: string) => request<TaskSnapshot>(`/api/v1/tasks/${encodeURIComponent(taskId)}`),
  create: (orderType: OrderType, text: string) =>
    request<TaskSummary>('/api/v1/tasks', {
      method: 'POST',
      body: JSON.stringify({ order_type: orderType, input: { text } }),
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
  patchDraft: (task: TaskSnapshot, patch: Partial<DraftCandidate>) => {
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
