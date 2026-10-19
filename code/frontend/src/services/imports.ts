import { useAuthStore } from '../stores/auth'

export type ImportCellIssue = {
  code: string
  field: string
  message: string
  locator: { sheet: string; row: number | null; column: number | null; cell: string | null }
}

export type ImportGroup = {
  group_id: string
  sheet: string
  rows: number[]
  status: 'READY_FOR_REVIEW' | 'NEEDS_CORRECTION' | 'OVER_QUOTA_REVIEW'
  issues: ImportCellIssue[]
  candidate: { items: unknown[]; [key: string]: unknown }
  total_amount: string | null
  quota?: { contract_amount: string; occupied_amount: string; remaining_before_group: string; requested_amount: string; over_quota: boolean }
}

export type ImportPreview = {
  groups: ImportGroup[]
  issues: ImportCellIssue[]
  summary: {
    sheet_count: number
    detail_rows: number
    group_count: number
    ready_groups: number
    over_quota_groups: number
    needs_correction_groups: number
  }
  source_refs: string[]
}

async function readError(response: Response) {
  const body = await response.json().catch(() => ({})) as { error?: { message?: string }; detail?: { message?: string } }
  return new Error(body.error?.message ?? body.detail?.message ?? `请求失败（HTTP ${response.status}）`)
}

export const importsApi = {
  async downloadTemplate() {
    const response = await fetch('/api/v1/imports/template', { credentials: 'same-origin' })
    if (!response.ok) throw await readError(response)
    return response.blob()
  },
  async upload(file: File): Promise<string> {
    const auth = useAuthStore()
    const csrf = auth.identity?.csrf_token
    if (!csrf) throw new Error('登录状态缺少安全校验信息，请刷新页面后重试。')
    const form = new FormData()
    form.set('parse_intent', 'order_xlsx')
    form.set('file', file)
    const upload = await fetch('/api/v1/artifacts', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf, 'X-Request-ID': crypto.randomUUID() },
      body: form,
    })
    if (!upload.ok) throw await readError(upload)
    const artifact = await upload.json() as { data: { artifact_id: string } }
    return artifact.data.artifact_id
  },
  async previewArtifact(artifactId: string): Promise<ImportPreview> {
    const auth = useAuthStore()
    const csrf = auth.identity?.csrf_token
    if (!csrf) throw new Error('登录状态缺少安全校验信息，请刷新页面后重试。')
    const response = await fetch('/api/v1/imports/preview', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
        'X-Request-ID': crypto.randomUUID(),
        'Idempotency-Key': crypto.randomUUID(),
      },
      body: JSON.stringify({ artifact_id: artifactId }),
    })
    if (!response.ok) throw await readError(response)
    const result = await response.json() as { data: ImportPreview }
    return result.data
  },
  async preview(file: File): Promise<ImportPreview> {
    const artifactId = await this.upload(file)
    return this.previewArtifact(artifactId)
  },
}
