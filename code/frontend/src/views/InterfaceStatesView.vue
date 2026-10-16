<script setup lang="ts">
import AppIcon from '../components/AppIcon.vue'
import DataTable from '../components/DataTable.vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { useUiStore } from '../stores/ui'

const ui = useUiStore()

const columns = [
  { key: 'state', label: '状态场景' },
  { key: 'feedback', label: '页面反馈' },
  { key: 'recovery', label: '安全恢复入口' },
]

const rows = [
  { id: 'loading', state: '处理中', feedback: '说明当前进度，避免重复提交', recovery: '返回或继续查看' },
  { id: 'empty', state: '空状态', feedback: '说明暂无数据及开始方式', recovery: '创建新任务' },
  { id: 'forbidden', state: '无权访问', feedback: '不暴露资源是否存在', recovery: '返回授权列表' },
  { id: 'conflict', state: '版本冲突', feedback: '提示版本已更新，保留本地编辑', recovery: '比较后刷新' },
]

const samples = [
  { icon: 'history', title: '正在解析文件', tone: 'info' as const, badge: '处理中', message: '页面需要展示明确状态与恢复入口。当前没有文件解析请求。', action: '示例操作' },
  { icon: 'info', title: '还没有提单任务', tone: 'neutral' as const, badge: '空状态', message: '说明如何开始。此卡片是通用组件示例，不代表当前账号有任务。', action: '返回工作台' },
  { icon: 'shield', title: '无权访问业务单据', tone: 'danger' as const, badge: '已阻止', message: '权限由服务端验证；不得借助修改 URL 或证据引用绕过授权。', action: '返回授权列表' },
  { icon: 'file', title: '文件无法解析', tone: 'danger' as const, badge: '需处理', message: '明确说明可恢复步骤，不把工具失败展示为成功。', action: '重新选择文件' },
  { icon: 'trace', title: '草稿版本已更新', tone: 'warning' as const, badge: '版本冲突', message: '保留未提交编辑，先比较最新版本，再由用户决定是否刷新。', action: '比较版本' },
  { icon: 'upload', title: '业务工具暂不可用', tone: 'warning' as const, badge: '依赖异常', message: '显示依赖错误和重试入口，不生成虚构的工具结果。', action: '检查服务状态' },
]

function showAction(action: string) {
  ui.notify(`「${action}」仅用于展示状态示例，当前没有执行业务操作。`)
}
</script>

<template>
  <section class="page-heading">
    <div>
      <p class="eyebrow">工作空间 / 界面状态与异常反馈</p>
      <h1>界面状态与异常反馈</h1>
      <p class="page-description">以下展示加载、空白、权限、冲突与错误组件；都是独立示例，不代表真实任务或业务数据。</p>
    </div>
    <RouterLink class="secondary-button" to="/"><AppIcon name="arrow" :size="17" />返回工作台</RouterLink>
  </section>

  <NoticeBanner tone="warning" message="此页面是界面状态组件示例，不会访问、创建或更改任务、草稿、合同或订单。" />

  <section class="state-grid" aria-label="通用界面状态示例">
    <article v-for="sample in samples" :key="sample.title" class="card state-card">
      <div class="state-card__top">
        <span class="state-card__icon" :class="`state-card__icon--${sample.tone}`"><AppIcon :name="sample.icon" :size="22" /></span>
        <StatusBadge :label="sample.badge" :tone="sample.tone" />
      </div>
      <h2>{{ sample.title }}</h2>
      <p>{{ sample.message }}</p>
      <button class="secondary-button" type="button" @click="showAction(sample.action)">{{ sample.action }}</button>
    </article>
  </section>

  <section class="card component-table-card">
    <div class="section-title-row">
      <div><h2>状态与恢复提示规范</h2><p>静态界面文案示例，不包含任务记录。</p></div>
      <StatusBadge label="组件示例" tone="info" />
    </div>
    <DataTable :columns="columns" :rows="rows" />
  </section>
</template>
