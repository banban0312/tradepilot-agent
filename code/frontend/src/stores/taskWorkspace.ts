import { defineStore } from 'pinia'
import { tasksApi, TASK_EVENT_TYPES, type DraftCandidate, type TaskSnapshot } from '../services/tasks'

export type WorkspaceEvent = {
  seq: number
  type: string
  emitted_at: string
  data: Record<string, unknown>
}

let eventSource: EventSource | null = null
let eventSourceTaskId = ''
let generation = 0
let refreshTimer: number | undefined

function editableCandidate(candidate: DraftCandidate) {
  return {
    contract_id: candidate.contract_id,
    customer_id: candidate.customer_id,
    project_id: candidate.project_id,
    currency: candidate.currency,
    delivery_date: candidate.delivery_date,
    address: candidate.address,
    contact: candidate.contact,
    business_fields: candidate.business_fields,
    items: candidate.items,
  }
}

function stopEventSource() {
  eventSource?.close()
  eventSource = null
  eventSourceTaskId = ''
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
  refreshTimer = undefined
}

export const useTaskWorkspaceStore = defineStore('task-workspace', {
  state: () => ({
    activeTaskId: null as string | null,
    task: null as TaskSnapshot | null,
    draftForm: null as DraftCandidate | null,
    savedDraftForm: '',
    replyText: '',
    activity: [] as WorkspaceEvent[],
    lastEventId: 0,
    loading: false,
    commandSubmitting: false,
    error: '',
    widgetOpen: false,
    widgetPosition: { left: 0, top: 0, placed: false },
    returnPath: '/orders',
  }),
  getters: {
    draftIsDirty(state) {
      if (!state.draftForm) return false
      return JSON.stringify(editableCandidate(state.draftForm)) !== state.savedDraftForm
    },
  },
  actions: {
    syncDraftFromSnapshot(snapshot: TaskSnapshot, force = false) {
      const candidate = snapshot.draft?.candidate
      if (!candidate) {
        this.draftForm = null
        this.savedDraftForm = ''
        return
      }
      if (!force && this.draftIsDirty) return
      this.draftForm = structuredClone(candidate)
      this.savedDraftForm = JSON.stringify(editableCandidate(this.draftForm))
    },

    async activateTask(taskId: string) {
      if (this.activeTaskId === taskId && this.task) {
        this.connectTaskEvents(taskId, generation)
        return this.task
      }
      const currentGeneration = ++generation
      stopEventSource()
      this.activeTaskId = taskId
      this.task = null
      this.draftForm = null
      this.savedDraftForm = ''
      this.replyText = ''
      this.activity = []
      this.lastEventId = 0
      this.error = ''
      this.loading = true
      try {
        const snapshot = await tasksApi.get(taskId)
        if (currentGeneration !== generation || this.activeTaskId !== taskId) return null
        this.task = snapshot
        this.syncDraftFromSnapshot(snapshot, true)
        this.connectTaskEvents(taskId, currentGeneration)
        return snapshot
      } catch (caught) {
        if (currentGeneration === generation) this.error = caught instanceof Error ? caught.message : '读取任务失败。'
        throw caught
      } finally {
        if (currentGeneration === generation) this.loading = false
      }
    },

    async refreshSnapshot(taskId?: string) {
      const currentTaskId = taskId ?? this.activeTaskId
      if (!currentTaskId || currentTaskId !== this.activeTaskId) return null
      const requestGeneration = generation
      const snapshot = await tasksApi.get(currentTaskId)
      if (requestGeneration !== generation || currentTaskId !== this.activeTaskId) return null
      const previousOrderId = this.task?.order_ref?.order_id
      this.task = snapshot
      this.syncDraftFromSnapshot(snapshot)
      if (!previousOrderId && snapshot.order_ref?.order_id) {
        window.dispatchEvent(new CustomEvent('tradepilot:order-created', { detail: { orderId: snapshot.order_ref.order_id } }))
      }
      return snapshot
    },

    async submitReply() {
      const current = this.task
      const text = this.replyText.trim()
      if (
        !current
        || !text
        || this.commandSubmitting
        || current.active_wait?.kind !== 'INPUT'
        || !current.available_actions.includes('INPUT')
      ) return false
      this.commandSubmitting = true
      try {
        await tasksApi.supplement(current, text)
        this.replyText = ''
        await this.refreshSnapshot(current.task_id)
        return true
      } catch (caught) {
        if ((caught as { status?: number }).status === 409) await this.refreshSnapshot(current.task_id)
        throw caught
      } finally {
        this.commandSubmitting = false
      }
    },

    connectTaskEvents(taskId: string, taskGeneration: number) {
      if (eventSource && eventSourceTaskId === taskId) return
      stopEventSource()
      if (taskId !== this.activeTaskId) return
      const source = new EventSource(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`)
      eventSource = source
      eventSourceTaskId = taskId
      const store = this
      const receive = (event: Event) => {
        if (taskGeneration !== generation || store.activeTaskId !== taskId) return
        const message = event as MessageEvent<string>
        try {
          const envelope = JSON.parse(message.data) as { seq?: number; emitted_at?: string; data?: Record<string, unknown> }
          const id = envelope.seq ?? (Number(message.lastEventId) || 0)
          if (id > 0 && id <= store.lastEventId) return
          if (id > 0) store.lastEventId = id
          const payload = envelope.data ?? {}
          const eventData = payload.data && typeof payload.data === 'object' ? payload.data as Record<string, unknown> : payload
          store.activity = [{ seq: id, type: event.type, emitted_at: envelope.emitted_at ?? new Date().toISOString(), data: eventData }, ...store.activity].slice(0, 12)
        } catch {
          const id = Number(message.lastEventId) || 0
          if (id > 0 && id <= store.lastEventId) return
          if (id > 0) store.lastEventId = id
          store.activity = [{ seq: id, type: event.type, emitted_at: new Date().toISOString(), data: {} }, ...store.activity].slice(0, 12)
        }
        if (refreshTimer === undefined) {
          refreshTimer = window.setTimeout(() => {
            refreshTimer = undefined
            void store.refreshSnapshot(taskId).catch((caught: unknown) => {
              store.error = caught instanceof Error ? caught.message : '刷新任务失败。'
            })
          }, 100)
        }
      }
      for (const type of TASK_EVENT_TYPES) source.addEventListener(type, receive)
      source.onerror = () => {
        // EventSource 自动使用 Last-Event-ID 重连；断线期间先读取服务端权威快照。
        void store.refreshSnapshot(taskId).catch((caught: unknown) => {
          store.error = caught instanceof Error ? caught.message : '刷新任务失败。'
        })
      }
    },

    setWidgetPosition(left: number, top: number, persist = true) {
      this.widgetPosition = { left, top, placed: true }
      if (persist) localStorage.setItem('tradepilot-widget-position', JSON.stringify({ left, top }))
    },

    restoreWidgetPosition() {
      try {
        const value = JSON.parse(localStorage.getItem('tradepilot-widget-position') ?? 'null') as { left?: number; top?: number } | null
        if (value && Number.isFinite(value.left) && Number.isFinite(value.top)) {
          this.widgetPosition = { left: value.left!, top: value.top!, placed: true }
        }
      } catch {
        localStorage.removeItem('tradepilot-widget-position')
      }
    },

    clearWorkspace() {
      generation += 1
      stopEventSource()
      this.$reset()
    },
  },
})
