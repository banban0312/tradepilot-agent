<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppDrawer from '../components/AppDrawer.vue'
import AppIcon from '../components/AppIcon.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ToastMessage from '../components/ToastMessage.vue'
import { roleLabels, useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'

const auth = useAuthStore()
const ui = useUiStore()
const route = useRoute()
const router = useRouter()
const helpOpen = ref(false)

const identity = computed(() => auth.identity)
const roleText = computed(() => identity.value?.roles.map((role) => roleLabels[role] ?? '受限角色').join('、') ?? '')
const userInitial = computed(() => identity.value?.login.slice(0, 1).toUpperCase() ?? 'U')
const pageTitle = computed(() => route.name === 'interface-states' ? '界面状态与异常反馈' : '开始一笔新订单')

const navigation = [
  { label: '智能提单', icon: 'sparkles', route: 'workbench' },
  { label: '订单中心', icon: 'file' },
  { label: '批量导入', icon: 'grid' },
  { label: '任务追踪', icon: 'trace' },
  { label: '评测中心', icon: 'chart' },
  { label: '主数据与配置', icon: 'sliders' },
]

function unavailable(label: string) {
  ui.notify(`${label}模块尚未接入，当前没有读取或展示业务数据。`)
}

async function logout() {
  if (!await auth.logout()) return
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <aside class="side-navigation" aria-label="主导航">
      <RouterLink class="brand brand--sidebar" to="/" aria-label="TradePilot 工作台">
        <span class="brand-mark"><span></span><span></span><span></span></span>
        <span>TradePilot</span>
      </RouterLink>
      <p class="side-navigation__caption">智能商贸提单助手</p>
      <nav class="main-nav">
        <template v-for="item in navigation" :key="item.label">
          <RouterLink v-if="item.route" :to="{ name: item.route }" class="main-nav__link" :class="{ active: route.name === item.route }">
            <AppIcon :name="item.icon" :size="18" /><span>{{ item.label }}</span>
          </RouterLink>
          <button v-else type="button" class="main-nav__link main-nav__link--future" @click="unavailable(item.label)">
            <AppIcon :name="item.icon" :size="18" /><span>{{ item.label }}</span>
          </button>
        </template>
        <RouterLink to="/interface-states" class="main-nav__link main-nav__link--secondary" :class="{ active: route.name === 'interface-states' }">
          <AppIcon name="info" :size="18" /><span>界面状态示例</span>
        </RouterLink>
      </nav>

      <div class="side-navigation__bottom">
        <div class="safety-card">
          <AppIcon name="shield" :size="19" />
          <strong>正式订单需要人工确认</strong>
          <small>产品规则：未经有效批准不创建正式订单</small>
        </div>
        <div class="identity-card">
          <span class="avatar">{{ userInitial }}</span>
          <div class="identity-card__text">
            <strong>{{ identity?.login }}</strong>
            <small>{{ roleText }} · {{ identity?.department_ids.length ?? 0 }} 个授权部门</small>
          </div>
          <button class="logout-button" type="button" :disabled="auth.loading" @click="logout">退出</button>
        </div>
      </div>
    </aside>

    <div class="workspace">
      <header class="top-bar">
        <div class="breadcrumbs"><span>工作空间</span><span aria-hidden="true">/</span><strong>{{ pageTitle }}</strong></div>
        <div class="top-bar__actions">
          <StatusBadge label="已登录" tone="success" />
          <button class="icon-button help-button" type="button" @click="helpOpen = true">
            <AppIcon name="info" :size="18" /><span>使用帮助</span>
          </button>
        </div>
      </header>
      <main class="workspace-content"><RouterView /></main>
    </div>

    <AppDrawer v-model="helpOpen" title="使用帮助">
      <ol class="help-steps">
        <li><strong>描述业务需求</strong><p>选择销售或采购，并说明合同、项目、物料、数量和交期。</p></li>
        <li><strong>补齐并核验</strong><p>缺失字段与物料歧义需由业务人员确认，页面提示不会替代服务端规则。</p></li>
        <li><strong>检查草稿与额度</strong><p>保存后重新校验合同、字段和履约余额。</p></li>
        <li><strong>人工批准后提交</strong><p>模型不能批准或创建正式订单；提交前会校验当前版本与权限。</p></li>
      </ol>
      <StatusBadge label="任务创建入口将在后续阶段接入" tone="warning" />
    </AppDrawer>
    <ToastMessage />
  </div>
</template>
