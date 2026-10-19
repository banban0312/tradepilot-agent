import { useAuthStore } from '../stores/auth'

export type ParseIntent = 'contract_pdf' | 'receipt_image' | 'order_xlsx'
export type Artifact = {
  artifact_id: string
  original_name: string
  media_type: string
  sha256: string
  byte_size: number
  state: string
  metadata: { parse_intent: ParseIntent; [key: string]: unknown }
  limits: { max_bytes: number; [key: string]: unknown }
  created_at: string
}
export type ParsedField = {
  field: string
  value: string
  page_number: number
  raw_text: string
  locator: { type: 'text_range' | 'bbox'; [key: string]: number | string }
  status?: 'CONFLICT' | 'NEEDS_CONFIRMATION'
  confirmation_required: boolean
  unit?: string
}
export type ParseResult = {
  state: 'COMPLETE' | 'PARTIAL'
  media_type: string
  elapsed_ms: number
  page_count?: number
  pages: Array<{ page_number: number; mode: string; lines: unknown[] }>
  fields: ParsedField[]
  missing_fields: string[]
  issues: Array<{ code: string; page_number?: number }>
}

async function errorMessage(response: Response) {
  const body = await response.json().catch(() => ({})) as {
    error?: { message?: string }
    detail?: { message?: string }
  }
  return new Error(body.error?.message ?? body.detail?.message ?? `请求失败（HTTP ${response.status}）`)
}

function mutationHeaders() {
  const csrf = useAuthStore().identity?.csrf_token
  if (!csrf) throw new Error('登录状态缺少安全校验信息，请刷新页面后重试。')
  return { 'X-CSRF-Token': csrf, 'X-Request-ID': crypto.randomUUID() }
}

export const artifactsApi = {
  async upload(file: File, intent: ParseIntent, taskId?: string): Promise<Artifact> {
    const form = new FormData()
    form.set('parse_intent', intent)
    form.set('file', file)
    if (taskId) form.set('task_id', taskId)
    const response = await fetch('/api/v1/artifacts', {
      method: 'POST', credentials: 'same-origin', headers: mutationHeaders(), body: form,
    })
    if (!response.ok) throw await errorMessage(response)
    return (await response.json() as { data: Artifact }).data
  },
  async parse(artifactId: string): Promise<ParseResult> {
    const headers = mutationHeaders()
    const response = await fetch(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/parse`, {
      method: 'POST', credentials: 'same-origin', headers,
    })
    if (!response.ok) throw await errorMessage(response)
    return (await response.json() as { data: ParseResult }).data
  },
  async listTask(taskId: string): Promise<Artifact[]> {
    const response = await fetch(`/api/v1/tasks/${encodeURIComponent(taskId)}/artifacts`, {
      credentials: 'same-origin', headers: { Accept: 'application/json' },
    })
    if (!response.ok) throw await errorMessage(response)
    return (await response.json() as { data: { items: Artifact[] } }).data.items
  },
  async content(artifactId: string): Promise<Blob> {
    const response = await fetch(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`, {
      credentials: 'same-origin',
    })
    if (!response.ok) throw await errorMessage(response)
    return response.blob()
  },
}
