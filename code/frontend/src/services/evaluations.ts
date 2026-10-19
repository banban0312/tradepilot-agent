import { useAuthStore } from '../stores/auth'

export type EvaluationCase = {
  case_id: string
  category: string
  status: 'PASS' | 'FAIL' | 'SKIPPED' | 'ERROR'
  expected: Record<string, unknown>
  actual: Record<string, unknown>
  failed_assertions: Array<{ source: string; expected: unknown; actual: unknown }>
  error_code: string | null
  task_id: string | null
  trace_id: string | null
}

export type EvaluationRun = {
  run_id: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  mode: 'offline' | 'online'
  dataset_version: string
  started_at: string | null
  completed_at: string | null
  progress: { completed: number; total: number; case_id?: string; case_status?: string }
  error_code: string | null
  summary?: {
    code_version?: string
    execution_metadata?: {
      provider?: string
      model?: string
      model_configuration?: string
      system_prompt_sha256?: string
    }
    case_count: number
    status_counts: Record<string, number>
    category_counts: Record<string, { applicable: number; PASS: number; FAIL: number; SKIPPED: number; ERROR: number; pass_rate: number | null }>
    metrics?: Record<string, unknown>
    safe_invariants?: { passed: number; applicable: number; pass_rate: number | null }
  }
  cases?: EvaluationCase[]
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.method && init.method !== 'GET' ? { 'X-CSRF-Token': auth.identity?.csrf_token ?? '' } : {}),
      ...init.headers,
    },
  })
  const body = (await response.json()) as { data?: T; error?: { message?: string }; detail?: string }
  if (!response.ok) throw new Error(body.error?.message ?? body.detail ?? '评测服务请求失败，请稍后重试。')
  return body.data as T
}

export const evaluationsApi = {
  list: () => request<EvaluationRun[]>('/api/v1/admin/eval-runs'),
  get: (runId: string) => request<EvaluationRun>('/api/v1/admin/eval-runs/' + encodeURIComponent(runId)),
  start: (mode: 'offline' | 'online') => request<EvaluationRun>('/api/v1/admin/eval-runs', { method: 'POST', body: JSON.stringify({ mode, dataset_version: 'v1' }) }),
  reportUrl: (runId: string, file: 'report.md' | 'summary.json' | 'cases.jsonl') => '/api/v1/admin/eval-runs/' + encodeURIComponent(runId) + '/report/' + file,
}
