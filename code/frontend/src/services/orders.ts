export type OrderSummary = {
  id: string
  number: string
  type: 'SALES' | 'PURCHASE'
  contract_number: string
  project_code: string
  project_name: string
  state: 'CREATED'
  currency: string
  total_amount: string
  created_at: string
  task_id: string
}

export type OrderList = { items: OrderSummary[]; total: number; limit: number; offset: number }
export type OrderDetail = {
  id: string
  number: string
  type: 'SALES' | 'PURCHASE'
  state: 'CREATED'
  department: { id: string; name: string }
  contract: {
    id: string; number: string; type: string; customer_name: string; project_id: string
    project_code: string; project_name: string; currency: string; total_amount: string
    occupied_amount: string | null; remaining_amount: string | null; balance_available: boolean
  }
  operator: { login: string; department_name: string }
  created_at: string
  delivery_date: string | null
  address: string | null
  contact: string | null
  business_fields: Record<string, unknown>
  currency: string
  total_amount: string
  items: Array<{
    line_no: number; material_code: string; material_name: string; raw_name: string | null
    specification: string | null; quantity: string; unit: string; unit_price: string; amount: string
  }>
  approval: {
    id: string; decision: string; version: number; content_hash: string; approved_by: string
    created_at: string; invalidated_at: string | null; snapshot_amount: string | null
  }
  sources: Array<{
    field_path: string; source_type: string
    locator: Record<string, unknown> | null; confirmed_by: string | null
  }>
  audit: Array<{
    action: string; result: string; actor: string; created_at: string; input_hash: string | null
  }>
  task: { id: string; state: string; trace_url: string }
}

type ApiEnvelope<T> = { data: T; error?: { code?: string; message?: string }; detail?: { code?: string; message?: string } }

async function request<T>(path: string): Promise<T> {
  const headers = new Headers({ Accept: 'application/json' })
  let response: Response
  try {
    response = await fetch(path, { headers, credentials: 'same-origin' })
  } catch {
    throw new Error('暂时无法连接订单服务，请检查 Docker 服务和网络。')
  }
  const body = await response.json() as ApiEnvelope<T>
  if (!response.ok) {
    const error = body.error ?? body.detail
    throw new Error(error?.message ?? `订单请求失败（HTTP ${response.status}），请刷新后重试。`)
  }
  return body.data
}

export const ordersApi = {
  list(filters: URLSearchParams) {
    return request<OrderList>(`/api/v1/orders?${filters.toString()}`)
  },
  get(orderId: string) {
    return request<OrderDetail>(`/api/v1/orders/${encodeURIComponent(orderId)}`)
  },
}
