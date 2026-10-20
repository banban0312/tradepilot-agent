import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'
import AppShell from './views/AppShell.vue'
import InterfaceStatesView from './views/InterfaceStatesView.vue'
import ImportBatchView from './views/ImportBatchView.vue'
import LoginView from './views/LoginView.vue'
import MasterDataView from './views/MasterDataView.vue'
import OrderCenterView from './views/OrderCenterView.vue'
import TaskWorkbenchView from './views/TaskWorkbenchView.vue'
import TaskHistoryView from './views/TaskHistoryView.vue'
import TaskTraceView from './views/TaskTraceView.vue'
import RuntimeSettingsView from './views/RuntimeSettingsView.vue'
import AuditLogView from './views/AuditLogView.vue'
import EvaluationCenterView from './views/EvaluationCenterView.vue'
import { useTaskWorkspaceStore } from './stores/taskWorkspace'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    {
      path: '/',
      component: AppShell,
      children: [
        { path: '', name: 'workbench', component: TaskWorkbenchView },
        { path: 'imports', name: 'batch-import', component: TaskWorkbenchView },
        { path: 'task-history', name: 'task-history', component: TaskHistoryView },
        { path: 'tasks/:taskId/trace', name: 'task-trace', component: TaskTraceView },
        { path: 'tasks/:taskId', name: 'task-workbench', component: TaskWorkbenchView },
        { path: 'imports/:batchId', name: 'import-batch', component: ImportBatchView },
        { path: 'orders', name: 'order-center', component: OrderCenterView },
        { path: 'admin/master-data', name: 'master-data', component: MasterDataView, meta: { requiresSystemAdmin: true } },
        { path: 'admin/runtime-settings', name: 'runtime-settings', component: RuntimeSettingsView, meta: { requiresSystemAdmin: true } },
        { path: 'admin/audit', name: 'audit-log', component: AuditLogView, meta: { requiresSystemAdmin: true } },
        { path: 'admin/evaluations', name: 'evaluation-center', component: EvaluationCenterView, meta: { requiresSystemAdmin: true } },
        { path: 'interface-states', name: 'interface-states', component: InterfaceStatesView },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to, from) => {
  const auth = useAuthStore()
  // 路由跳转时重新询问服务端，避免客户端缓存身份掩盖已过期会话。
  await auth.loadIdentity(true)

  if (to.meta.public) {
    if (to.name === 'login' && auth.identity) return { name: 'workbench' }
    return true
  }

  if (!auth.identity) {
    useTaskWorkspaceStore().clearWorkspace()
    return {
      name: 'login',
      query: {
        redirect: to.fullPath,
        ...(auth.errorCode === 'SESSION_EXPIRED' ? { reason: 'expired' } : {}),
      },
    }
  }

  if (to.meta.requiresSystemAdmin && !auth.identity.roles.includes('system_admin')) {
    return { name: 'workbench' }
  }

  if (to.name === 'task-workbench' && from.name !== 'task-workbench') {
    useTaskWorkspaceStore().returnPath = from.name === 'login' ? '/orders' : (from.fullPath || '/orders')
  }

  return true
})
