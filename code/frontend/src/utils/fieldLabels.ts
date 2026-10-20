/** 等待点既包含历史字段路径，也包含服务端返回的结构化校验问题。 */
export type MissingField = string | { field: string; message?: string }

export function fieldLabel(field: MissingField): string {
  if (typeof field !== 'string') {
    const label = fieldLabel(field.field)
    return field.message ? `${label}：${field.message}` : label
  }
  const path = field
  const labels: Record<string, string> = {
    order_type: '订单类型', contract_id: '合同', customer_id: '客户', project_id: '项目', currency: '币种',
    delivery_date: '交期', address: '收货地址', contact: '联系人', total_amount: '订单金额',
  }
  const itemMatch = path.match(/^items\[(\d+)](?:\.(.+))?$/)
  if (itemMatch) {
    const itemName: Record<string, string> = { material_id: '物料', raw_name: '物料名称', specification: '规格', quantity: '数量', unit: '单位', unit_price: '单价' }
    return `第 ${Number(itemMatch[1]) + 1} 项${itemMatch[2] ? ` · ${itemName[itemMatch[2]] ?? itemMatch[2]}` : ''}`
  }
  return labels[path] ?? path
}
