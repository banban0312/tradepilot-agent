import { useAuthStore } from '../stores/auth'

export type Department = { id: string; code: string; name: string }
export type LookupItem = { id: string; code: string; name: string }
export type MaterialLookup = LookupItem & { specification: string; unit: string }
export type ContractLine = {
  id: string
  material_id: string
  material_code: string
  material_name: string
  specification: string
  unit: string
  unit_price: string | null
  max_quantity: string | null
}
export type ContractRecord = {
  id: string
  department_id: string
  department_name: string
  number: string
  type: 'SALES' | 'PURCHASE'
  customer_id: string
  customer_name: string
  project_id: string
  project_name: string
  currency: string
  total_amount: string
  status: 'ACTIVE' | 'INACTIVE'
  valid_from: string
  valid_to: string
  version: number
  occupied_amount: string
  order_count: number
  remaining_amount: string
  lines: ContractLine[]
  created_at: string
  updated_at: string
}
export type MaterialRecord = {
  id: string
  code: string
  name: string
  specification: string
  unit: string
  active: boolean
  version: number
  aliases: string[]
  created_at: string
  updated_at: string
}
export type Page<T> = { items: T[]; total: number; limit: number; offset: number }
export type ContractLookups = {
  customers: LookupItem[]
  projects: LookupItem[]
  materials: MaterialLookup[]
}
export type MatchPreview = {
  candidates: Array<{
    material_id: string
    code: string
    name: string
    specification: string
    unit: string
    reason: string
    requires_confirmation: boolean
  }>
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined) headers.set('Content-Type', 'application/json')
  if (init.method && init.method !== 'GET') {
    const token = auth.identity?.csrf_token
    if (!token) throw new Error('当前登录状态缺少安全校验信息，请刷新后重试。')
    headers.set('X-CSRF-Token', token)
  }
  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch {
    throw new Error('暂时无法连接主数据服务，请检查网络后重试。')
  }
  const body = (await response.json()) as {
    data?: T
    error?: { code?: string; message?: string }
  }
  if (!response.ok) {
    if (response.status === 409) throw new Error(body.error?.message ?? '记录已变化，请刷新后重试。')
    if (response.status === 403) throw new Error('当前账号没有主数据管理权限。')
    throw new Error(body.error?.message ?? '主数据请求失败，请稍后重试。')
  }
  return body.data as T
}

export const masterDataApi = {
  departments: () => request<Department[]>('/api/v1/admin/departments'),
  lookups: (departmentId: string) =>
    request<ContractLookups>(`/api/v1/admin/lookups?department_id=${encodeURIComponent(departmentId)}`),
  contracts: (filters: URLSearchParams) =>
    request<Page<ContractRecord>>(`/api/v1/admin/contracts?${filters.toString()}`),
  contract: (id: string) => request<ContractRecord>(`/api/v1/admin/contracts/${encodeURIComponent(id)}`),
  createContract: (value: Record<string, unknown>) =>
    request<ContractRecord>('/api/v1/admin/contracts', { method: 'POST', body: JSON.stringify(value) }),
  updateContract: (id: string, value: Record<string, unknown>) =>
    request<ContractRecord>(`/api/v1/admin/contracts/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(value) }),
  materials: (filters: URLSearchParams) =>
    request<Page<MaterialRecord>>(`/api/v1/admin/materials?${filters.toString()}`),
  createMaterial: (value: Record<string, unknown>) =>
    request<MaterialRecord>('/api/v1/admin/materials', { method: 'POST', body: JSON.stringify(value) }),
  updateMaterial: (id: string, value: Record<string, unknown>) =>
    request<MaterialRecord>(`/api/v1/admin/materials/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(value) }),
  previewMatch: (rawName: string) =>
    request<MatchPreview>(`/api/v1/admin/materials/match-preview?raw_name=${encodeURIComponent(rawName)}`),
}
