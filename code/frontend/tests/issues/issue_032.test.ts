import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { DraftCandidate, TaskSnapshot } from '../../src/services/tasks'
import { useTaskWorkspaceStore } from '../../src/stores/taskWorkspace'

const serviceMocks = vi.hoisted(() => ({ get: vi.fn(), supplement: vi.fn() }))

vi.mock('../../src/services/tasks', () => ({
  tasksApi: { get: serviceMocks.get, supplement: serviceMocks.supplement },
  TASK_EVENT_TYPES: ['task.status_changed', 'draft.updated'],
}))

class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly listeners = new Map<string, Array<(event: Event) => void>>()
  closed = false
  onerror: ((event: Event) => void) | null = null

  constructor(readonly url: string) { FakeEventSource.instances.push(this) }
  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const handlers = this.listeners.get(type) ?? []
    handlers.push(listener as (event: Event) => void)
    this.listeners.set(type, handlers)
  }
  close() { this.closed = true }
  emit(type: string, seq: number) {
    const event = { type, lastEventId: String(seq), data: JSON.stringify({ seq, data: { state: 'WAITING_INPUT' } }) } as MessageEvent<string>
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

const candidate = (): DraftCandidate => ({
  order_type: 'SALES',
  contract_id: 'contract-1',
  customer_id: 'customer-1',
  project_id: 'project-1',
  currency: 'CNY',
  delivery_date: '2026-10-20',
  address: '服务器地址',
  contact: '测试联系人',
  business_fields: {},
  items: [],
  evidence: [],
})

function snapshot(taskId = 'task-1'): TaskSnapshot {
  return {
    task_id: taskId,
    session_id: 'session-1',
    order_type: 'SALES',
    state: 'WAITING_INPUT',
    projection_version: 1,
    current_step: 'prepare_input_wait',
    inputs: [{ command_id: 'input-1', role: 'user', text: '建立订单草稿', created_at: '2026-10-19T00:00:00Z' }],
    draft: {
      draft_id: 'draft-1', version: 1, content_hash: 'a'.repeat(64), total_amount: '100.00', validated: false, issues: [], candidate: candidate(),
    },
    quota_preview: null,
    active_wait: { kind: 'INPUT', checkpoint_id: 'checkpoint-1', interrupt_id: 'interrupt-1', draft_version: 1, content_hash: 'a'.repeat(64), missing_fields: ['收货地址'], reason: null },
    latest_review: null,
    order_ref: null,
    last_error: null,
    available_actions: ['INPUT', 'EDIT_DRAFT'],
    created_at: '2026-10-19T00:00:00Z',
    updated_at: '2026-10-19T00:00:00Z',
  }
}

describe('Issue #32 全屏与悬浮窗共享任务状态', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.stubGlobal('localStorage', { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() })
    vi.stubGlobal('window', {
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      dispatchEvent: vi.fn(),
    })
    setActivePinia(createPinia())
  })

  it('切换同一任务时复用快照和事件流，保留未发送内容与未保存草稿', async () => {
    const initial = snapshot()
    serviceMocks.get.mockResolvedValueOnce(initial)
    const workspace = useTaskWorkspaceStore()
    await workspace.activateTask('task-1')
    workspace.replyText = '尚未发送的补充内容'
    workspace.draftForm!.address = '本地未保存地址'

    const updated = snapshot()
    updated.projection_version = 2
    updated.draft!.version = 2
    updated.draft!.candidate!.address = '服务端新地址'
    serviceMocks.get.mockResolvedValueOnce(updated)
    await workspace.refreshSnapshot()
    await workspace.activateTask('task-1')

    expect(workspace.replyText).toBe('尚未发送的补充内容')
    expect(workspace.draftForm?.address).toBe('本地未保存地址')
    expect(serviceMocks.get).toHaveBeenCalledTimes(2)
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('事件 ID 去重，离开全屏仍保持单一事件流，登出时关闭连接', async () => {
    serviceMocks.get.mockResolvedValue(snapshot())
    const workspace = useTaskWorkspaceStore()
    await workspace.activateTask('task-1')
    const source = FakeEventSource.instances[0]

    source.emit('task.status_changed', 4)
    source.emit('task.status_changed', 4)
    expect(workspace.activity).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(100)
    expect(serviceMocks.get).toHaveBeenCalledTimes(2)
    await workspace.activateTask('task-1')
    expect(FakeEventSource.instances).toHaveLength(1)

    workspace.clearWorkspace()
    expect(source.closed).toBe(true)
    vi.useRealTimers()
  })

  it('补充信息由共享发送锁提交一次，并在成功后清空输入', async () => {
    serviceMocks.get.mockResolvedValue(snapshot())
    serviceMocks.supplement.mockResolvedValue(undefined)
    const workspace = useTaskWorkspaceStore()
    await workspace.activateTask('task-1')
    workspace.replyText = '补充后的收货地址'

    const result = await workspace.submitReply()

    expect(result).toBe(true)
    expect(serviceMocks.supplement).toHaveBeenCalledTimes(1)
    expect(serviceMocks.supplement).toHaveBeenCalledWith(workspace.task, '补充后的收货地址')
    expect(workspace.replyText).toBe('')
    expect(workspace.commandSubmitting).toBe(false)
  })

  it('审批等待继续绑定同一任务与草稿版本，悬浮窗不越权提交审批', async () => {
    const approval = snapshot()
    approval.state = 'WAITING_APPROVAL'
    approval.available_actions = ['APPROVE']
    approval.active_wait = {
      kind: 'APPROVAL', checkpoint_id: 'approval-checkpoint', interrupt_id: 'approval-interrupt',
      draft_version: 1, content_hash: 'a'.repeat(64), missing_fields: [], reason: null,
    }
    serviceMocks.get.mockResolvedValue(approval)
    const workspace = useTaskWorkspaceStore()
    await workspace.activateTask('task-1')
    workspace.replyText = '这段内容不能提交为审批决定'

    const result = await workspace.submitReply()

    expect(result).toBe(false)
    expect(workspace.task?.task_id).toBe('task-1')
    expect(workspace.task?.active_wait?.kind).toBe('APPROVAL')
    expect(workspace.task?.active_wait?.draft_version).toBe(1)
    expect(workspace.replyText).toBe('这段内容不能提交为审批决定')
    expect(serviceMocks.supplement).not.toHaveBeenCalled()
  })
})
