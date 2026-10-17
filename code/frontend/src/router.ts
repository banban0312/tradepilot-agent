import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'
import AppShell from './views/AppShell.vue'
import InterfaceStatesView from './views/InterfaceStatesView.vue'
import LoginView from './views/LoginView.vue'
import MasterDataView from './views/MasterDataView.vue'
import TaskWorkbenchView from './views/TaskWorkbenchView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    {
      path: '/',
      component: AppShell,
      children: [
        { path: '', name: 'workbench', component: TaskWorkbenchView },
        { path: 'tasks/:taskId', name: 'task-workbench', component: TaskWorkbenchView },
        { path: 'admin/master-data', name: 'master-data', component: MasterDataView, meta: { requiresSystemAdmin: true } },
        { path: 'interface-states', name: 'interface-states', component: InterfaceStatesView },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // 路由跳转时重新询问服务端，避免客户端缓存身份掩盖已过期会话。
  await auth.loadIdentity(true)

  if (to.meta.public) {
    if (to.name === 'login' && auth.identity) return { name: 'workbench' }
    return true
  }

  if (!auth.identity) {
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

  return true
})
