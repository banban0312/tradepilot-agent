import { useAuthStore } from '../stores/auth'

export type RuntimeConfig = {
  version: number
  content_hash: string
  content: {
    model_provider: 'openai' | 'deepseek' | null
    model_name: string | null
    model_base_url: string | null
    model_timeout_seconds: number
    model_max_retries: number
    model_max_rounds: number
    model_max_tool_calls: number
    max_concurrency: number
    ocr_provider: string | null
    mcp_url: string
    business_timezone: string
    trace_retention_days: number
    artifact_retention_days: number
    approval_mode: 'human_required' | 'demo_self_approval'
  }
  model_api_key_configured: boolean
  ocr_api_key_configured: boolean
  secret_storage_available: boolean
  created_by: string
  created_at: string | null
}

export type AuditRecord = {
  id: string
  task_id: string | null
  actor_id: string | null
  actor: string
  department: string
  action: string
  result: string
  object_ref: Record<string, unknown> | null
  before_ref: Record<string, unknown> | null
  after_ref: Record<string, unknown> | null
  input_hash: string | null
  created_at: string
}

export type RuntimeConfigUpdate = RuntimeConfig['content'] & {
  expected_version: number
  model_api_key?: string
  ocr_api_key?: string
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.method && init.method !== 'GET' ? { 'X-CSRF-Token': auth.identity?.csrf_token ?? '' } : {}),
      ...init.headers,
    },
  })
  const body = (await response.json()) as { data?: T; error?: { message?: string }; detail?: string }
  if (!response.ok) throw new Error(body.error?.message ?? body.detail ?? '请求失败，请重试。')
  return body.data as T
}

export const adminConfigApi = {
  config: () => request<RuntimeConfig>('/api/v1/admin/config'),
  update: (value: RuntimeConfigUpdate) =>
    request<Omit<RuntimeConfig, 'model_api_key_configured' | 'ocr_api_key_configured' | 'secret_storage_available' | 'created_by' | 'created_at'>>('/api/v1/admin/config', { method: 'PUT', body: JSON.stringify(value) }),
  versions: () => request<Array<{ version: number; content: RuntimeConfig['content']; content_hash: string; created_by: string; created_at: string; is_current: boolean }>>('/api/v1/admin/config/versions'),
  audit: (params: URLSearchParams) => request<{ items: AuditRecord[]; next_cursor: string | null }>(`/api/v1/admin/audit?${params.toString()}`),
}
