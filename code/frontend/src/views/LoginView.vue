<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppDrawer from '../components/AppDrawer.vue'
import AppIcon from '../components/AppIcon.vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const loginName = ref(typeof route.query.login === 'string' ? route.query.login : 'demo-operator-north')
const password = ref('')
const helpOpen = ref(false)
const expired = computed(() => route.query.reason === 'expired')

const demoAccounts = [
  { label: '经办人', login: 'demo-operator-north' },
  { label: '审批人', login: 'demo-approver-north' },
  { label: '管理员', login: 'demo-admin' },
]

async function submit() {
  const accepted = await auth.login(loginName.value.trim(), password.value)
  if (!accepted) return
  password.value = ''
  const target = route.query.redirect
  const safeTarget = typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')
    ? target
    : '/'
  await router.replace(safeTarget)
}
</script>

<template>
  <main class="login-page">
    <section class="login-intro" aria-label="TradePilot 产品介绍">
      <RouterLink class="brand brand--light" to="/login" aria-label="TradePilot 登录页">
        <span class="brand-mark"><span></span><span></span><span></span></span>
        <span>TradePilot</span>
      </RouterLink>
      <div class="login-intro__main">
        <p class="eyebrow eyebrow--light">智能商贸提单助手</p>
        <h1>智能商贸<br />提单工作空间</h1>
        <p class="login-intro__summary">面向业务提单场景设计，<br />逐步建立可确认、可恢复、可追溯的工作流程。</p>
        <p class="login-benefits__caption">产品设计目标 · 按项目阶段逐步接入</p>
        <ul class="login-benefits">
          <li><AppIcon name="trace" :size="21" /><span><strong>可恢复流程</strong><small>目标：补充信息或审批后继续原任务</small></span></li>
          <li><AppIcon name="shield" :size="21" /><span><strong>提交有约束</strong><small>后续接入服务端权限、额度与人工审批校验</small></span></li>
          <li><AppIcon name="file" :size="21" /><span><strong>来源可核验</strong><small>目标：从文本、PDF、图片及 Excel 提取字段来源</small></span></li>
        </ul>
      </div>
      <p class="login-intro__foot">当前为 TradePilot 工作空间登录入口</p>
    </section>

    <section class="login-panel">
      <div class="login-card">
        <span class="workspace-label">安全工作空间</span>
        <h2>登录 TradePilot</h2>
        <p class="login-lead">使用你的业务身份进入授权部门的工作空间。</p>

        <NoticeBanner
          v-if="expired"
          class="login-notice"
          tone="warning"
          message="登录已过期。重新登录后可继续查看授权范围内的页面。"
        />
        <NoticeBanner
          v-if="auth.error"
          class="login-notice"
          tone="danger"
          :message="auth.error"
        />

        <form class="login-form" @submit.prevent="submit">
          <label for="login-name">账号</label>
          <input id="login-name" v-model="loginName" autocomplete="username" required maxlength="64" placeholder="输入业务账号" />
          <label for="login-password">密码</label>
          <input id="login-password" v-model="password" type="password" autocomplete="current-password" required maxlength="256" placeholder="输入密码" />
          <div class="login-form__links">
            <span>会话有效期由服务端配置</span>
            <button class="text-button" type="button" @click="helpOpen = true">登录帮助</button>
          </div>
          <button class="primary-button login-submit" type="submit" :disabled="auth.loading">
            <AppIcon v-if="!auth.loading" name="arrow" :size="18" />
            <span>{{ auth.loading ? '正在验证身份…' : '登录工作空间' }}</span>
          </button>
        </form>

        <div class="demo-account-picker">
          <h3>切换示例身份</h3>
          <div class="demo-account-picker__buttons">
            <button
              v-for="account in demoAccounts"
              :key="account.login"
              type="button"
              :class="{ selected: loginName === account.login }"
              @click="loginName = account.login"
            >{{ account.label }}</button>
          </div>
          <p>示例账号仅在显式启用演示模式后可登录；密码由本机环境配置，不在页面中显示。</p>
        </div>
      </div>
    </section>

    <AppDrawer v-model="helpOpen" title="登录帮助">
      <h3>使用业务账号登录</h3>
      <p>登录账号、密码和部门范围由服务端验证。示例身份只在演示模式启用时可使用。</p>
      <h3>遇到登录失败</h3>
      <p>请检查账号密码、网络和服务状态。会话过期后重新登录即可；页面不会在浏览器本地保存密码或会话令牌。</p>
    </AppDrawer>
  </main>
</template>
