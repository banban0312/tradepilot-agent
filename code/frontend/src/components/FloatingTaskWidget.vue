<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from './AppIcon.vue'
import StatusBadge from './StatusBadge.vue'
import { useTaskWorkspaceStore } from '../stores/taskWorkspace'
import { tasksApi, type TaskSummary } from '../services/tasks'

const route = useRoute()
const router = useRouter()
const workspace = useTaskWorkspaceStore()
const { task, replyText, widgetOpen, widgetPosition, commandSubmitting } = storeToRefs(workspace)
const panel = ref<HTMLElement | null>(null)
const recentTasks = ref<TaskSummary[]>([])
const loadingRecentTasks = ref(false)
const recentTasksLoaded = ref(false)
const recentTasksError = ref('')
const widgetVisible = computed(() => route.name !== 'task-workbench')
const inputWait = computed(() => task.value?.active_wait?.kind === 'INPUT' && task.value.available_actions.includes('INPUT'))
const approvalWait = computed(() => task.value?.active_wait?.kind === 'APPROVAL')
const widgetError = ref('')
let dragOffset = { x: 0, y: 0 }

function statusLabel(state: string) {
  const labels: Record<string, string> = {
    QUEUED: '排队中', RUNNING: '处理中', WAITING_INPUT: '等待补充', WAITING_APPROVAL: '等待审批',
    SUBMITTING: '结果核实中', BLOCKED_RULE: '规则阻断', FAILED: '处理失败', COMPLETED: '已完成', REJECTED: '已拒绝',
  }
  return labels[state] ?? state
}

function ensurePosition() {
  if (widgetPosition.value.placed) {
    clampPosition(widgetPosition.value.left, widgetPosition.value.top)
    return
  }
  const width = Math.min(390, window.innerWidth - 24)
  const height = Math.min(530, window.innerHeight - 24)
  workspace.setWidgetPosition(Math.max(12, window.innerWidth - width - 24), Math.max(12, window.innerHeight - height - 24))
}

function clampPosition(left: number, top: number, persist = true) {
  const width = panel.value?.offsetWidth ?? Math.min(390, window.innerWidth - 24)
  const height = panel.value?.offsetHeight ?? Math.min(530, window.innerHeight - 24)
  workspace.setWidgetPosition(
    Math.min(Math.max(8, left), Math.max(8, window.innerWidth - width - 8)),
    Math.min(Math.max(8, top), Math.max(8, window.innerHeight - height - 8)),
    persist,
  )
}

function beginDrag(event: PointerEvent) {
  if ((event.target as HTMLElement).closest('button')) return
  dragOffset = { x: event.clientX - widgetPosition.value.left, y: event.clientY - widgetPosition.value.top }
  document.addEventListener('pointermove', drag)
  document.addEventListener('pointerup', endDrag, { once: true })
}

function drag(event: PointerEvent) {
  clampPosition(event.clientX - dragOffset.x, event.clientY - dragOffset.y, false)
}

function endDrag() {
  document.removeEventListener('pointermove', drag)
  if (widgetPosition.value.placed) workspace.setWidgetPosition(widgetPosition.value.left, widgetPosition.value.top)
}

async function loadRecentTasks(force = false) {
  if (loadingRecentTasks.value || (recentTasksLoaded.value && !force)) return
  loadingRecentTasks.value = true
  recentTasksError.value = ''
  try {
    recentTasks.value = (await tasksApi.list()).items.slice(0, 5)
    recentTasksLoaded.value = true
  } catch (caught) {
    recentTasksError.value = caught instanceof Error ? caught.message : '读取最近任务失败。'
  } finally {
    loadingRecentTasks.value = false
  }
}

function showWidget() {
  widgetOpen.value = true
  if (!task.value) void loadRecentTasks()
}

async function openFullScreen() {
  widgetError.value = ''
  workspace.returnPath = route.fullPath
  widgetOpen.value = false
  if (workspace.activeTaskId) {
    await router.push({ name: 'task-workbench', params: { taskId: workspace.activeTaskId } })
  } else {
    await router.push({ name: 'workbench' })
  }
}

async function openRecentTask(taskId: string) {
  workspace.returnPath = route.fullPath
  widgetOpen.value = false
  await router.push({ name: 'task-workbench', params: { taskId } })
}

async function sendReply() {
  const current = task.value
  const text = replyText.value.trim()
  if (!current || !text || !inputWait.value || commandSubmitting.value) return
  widgetError.value = ''
  try {
    await workspace.submitReply()
  } catch (caught) {
    widgetError.value = caught instanceof Error ? caught.message : '补充信息失败。'
  }
}

function onResize() {
  if (widgetPosition.value.placed) clampPosition(widgetPosition.value.left, widgetPosition.value.top)
}

onMounted(() => {
  workspace.restoreWidgetPosition()
  ensurePosition()
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  endDrag()
  document.removeEventListener('pointerup', endDrag)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <template v-if="widgetVisible">
    <button v-if="!widgetOpen" class="floating-task-bubble" type="button" aria-label="展开智能提单悬浮窗" @click="showWidget">
      <AppIcon name="sparkles" :size="21" />
      <span v-if="task && ['WAITING_INPUT', 'WAITING_APPROVAL'].includes(task.state)" class="floating-task-bubble__dot"></span>
    </button>

    <section v-else ref="panel" class="floating-task-panel" :style="{ left: `${widgetPosition.left}px`, top: `${widgetPosition.top}px` }" aria-label="智能提单悬浮窗">
      <header class="floating-task-panel__header" @pointerdown="beginDrag">
        <div><AppIcon name="sparkles" :size="17" /><strong>智能提单</strong></div>
        <div class="floating-task-panel__actions">
          <button type="button" aria-label="转为全屏工作台" title="转为全屏工作台" @click="openFullScreen">↗</button>
          <button type="button" aria-label="收起悬浮窗" title="收起悬浮窗" @click="widgetOpen = false">−</button>
        </div>
      </header>

      <div class="floating-task-panel__body">
        <template v-if="task">
          <div class="floating-task-panel__taskline">
            <span>任务 {{ task.task_id.slice(0, 8) }}</span>
            <StatusBadge :label="statusLabel(task.state)" :tone="approvalWait || inputWait ? 'warning' : task.state === 'COMPLETED' ? 'success' : task.state === 'FAILED' || task.state === 'BLOCKED_RULE' ? 'danger' : 'info'" />
          </div>
          <div class="floating-task-panel__messages" aria-live="polite">
            <article v-for="message in task.inputs.slice(-4)" :key="message.command_id" class="floating-task-message floating-task-message--user">
              <span>业务需求</span><p>{{ message.text }}</p>
            </article>
            <article v-if="approvalWait" class="floating-task-message floating-task-message--notice">
              <span>等待人工审批</span><p>草稿 v{{ task.active_wait?.draft_version ?? task.draft?.version ?? '—' }} 已进入审批等待。打开全屏工作台检查内容并处理审批。</p>
            </article>
            <article v-else-if="inputWait" class="floating-task-message floating-task-message--notice">
              <span>需要补充信息</span><p>{{ task.active_wait?.missing_fields.length ? `请补充：${task.active_wait.missing_fields.join('、')}` : '任务正在等待你补充业务信息。' }}</p>
            </article>
            <article v-else-if="task.order_ref" class="floating-task-message floating-task-message--notice">
              <span>正式订单已生成</span><p>{{ task.order_ref.order_number }} · {{ task.order_ref.total_amount }} {{ task.order_ref.currency }}</p>
            </article>
            <article v-else class="floating-task-message floating-task-message--notice">
              <span>任务状态</span><p>{{ task.state === 'COMPLETED' ? '任务已完成。' : task.state === 'FAILED' ? '任务处理失败，请打开全屏工作台查看失败位置。' : '后台任务继续运行；关闭悬浮窗不会终止任务。' }}</p>
            </article>
          </div>
          <div v-if="task.draft" class="floating-task-panel__draft">{{ task.order_type === 'SALES' ? '销售' : '采购' }}订单草稿 v{{ task.draft.version }}<strong>{{ task.draft.total_amount ?? '金额待核算' }} {{ task.draft.candidate?.currency ?? '' }}</strong></div>
          <form v-if="inputWait" class="floating-task-panel__composer" @submit.prevent="sendReply">
            <textarea v-model="replyText" rows="2" maxlength="12000" aria-label="补充任务信息" placeholder="继续补充订单信息…" />
            <button class="primary-button" type="submit" :disabled="commandSubmitting || !replyText.trim()">{{ commandSubmitting ? '发送中…' : '发送' }}</button>
          </form>
          <p v-else class="floating-task-panel__hint">{{ approvalWait ? '审批操作请在全屏工作台完成。' : '全屏与悬浮窗共享同一任务；收起后任务仍会继续。' }}</p>
          <p v-if="widgetError" class="floating-task-panel__error" role="alert">{{ widgetError }}</p>
          <button class="floating-task-panel__open" type="button" @click="openFullScreen">{{ approvalWait ? '打开全屏处理审批' : '打开全屏工作台' }}<AppIcon name="arrow" :size="15" /></button>
        </template>
        <div v-else class="floating-task-panel__empty">
          <AppIcon name="history" :size="24" /><strong>继续处理任务</strong>
          <p>选择最近任务，或进入工作台创建新任务。</p>
          <p v-if="loadingRecentTasks" role="status">正在读取最近任务…</p>
          <p v-else-if="recentTasksError" role="alert">{{ recentTasksError }}</p>
          <div v-else-if="recentTasks.length" class="floating-task-panel__recent-list">
            <button v-for="item in recentTasks" :key="item.task_id" type="button" @click="openRecentTask(item.task_id)">
              <strong>{{ item.order_type === 'SALES' ? '销售' : '采购' }} · {{ item.task_id.slice(0, 8) }}</strong>
              <span>{{ statusLabel(item.state) }} · {{ item.draft_version ? `草稿 v${item.draft_version}` : '尚无草稿' }}</span>
            </button>
          </div>
          <button v-if="recentTasksError" class="text-button" type="button" @click="loadRecentTasks(true)">重试读取</button>
          <button class="primary-button" type="button" @click="openFullScreen">打开工作台</button>
        </div>
      </div>
      <footer class="floating-task-panel__footer">工作台与悬浮窗共享任务、草稿与未发送内容</footer>
    </section>
  </template>
</template>
