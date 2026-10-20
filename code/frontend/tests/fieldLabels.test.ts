import { describe, expect, it } from 'vitest'
import { fieldLabel } from '../src/utils/fieldLabels'

describe('任务等待点的字段反馈', () => {
  it('显示历史字符串字段', () => {
    expect(fieldLabel('address')).toBe('收货地址')
  })
  it('显示结构化校验问题，避免任务渲染时调用对象的 match', () => {
    expect(fieldLabel({ field: 'items[0].unit_price', message: '单价与合同不一致' }))
      .toBe('第 1 项 · 单价：单价与合同不一致')
  })
  it('结构化字段没有说明时仍能正确显示', () => {
    expect(fieldLabel({ field: 'contact' })).toBe('联系人')
  })
  it('保留未知字段路径，便于定位', () => {
    expect(fieldLabel('business_fields.reference')).toBe('business_fields.reference')
  })
  it('将合同物料校验字段显示为中文', () => {
    expect(fieldLabel({ field: 'items[0].material_id', message: '所选物料未配置在该合同中' }))
      .toBe('第 1 项 · 物料：所选物料未配置在该合同中')
  })
})
