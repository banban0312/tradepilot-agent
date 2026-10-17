<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ordersApi, type OrderDetail, type OrderSummary } from '../services/orders'

const route = useRoute()
const router = useRouter()
const rows = ref<OrderSummary[]>([])
const detail = ref<OrderDetail | null>(null)
const selectedOrderId = ref<string | null>(null)
const loading = ref(false)
const detailLoading = ref(false)
const errorMessage = ref('')
const detailError = ref('')
const total = ref(0)
const offset = ref(0)
const pageSize = 20
const type = ref('')
const contract = ref('')
const project = ref('')
const state = ref('')
const from = ref('')
const to = ref('')
const dateToday = new Date().toISOString().slice(0, 10)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const currentPage = computed(() => Math.floor(offset.value / pageSize) + 1)

function formatMoney(value: string, currency: string) {
  const amount = Number(value)
  if (!Number.isFinite(amount)) return `${value} ${currency}`
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency, minimumFractionDigits: 2 }).format(amount)
}

function formatTime(value: string) {
  const time = new Date(value)
  return Number.isNaN(time.valueOf()) ? value : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(time)
}

function typeLabel(value: string) { return value === 'SALES' ? '销售' : '采购' }
function stateLabel(value: string) { return value === 'CREATED' ? '已生成' : value }

async function loadOrders(reset = false) {
  if (reset) offset.value = 0
  loading.value = true
  errorMessage.value = ''
  const query = new URLSearchParams({ limit: String(pageSize), offset: String(offset.value) })
  if (type.value) query.set('type', type.value)
  if (contract.value.trim()) query.set('contract', contract.value.trim())
  if (project.value.trim()) query.set('project', project.value.trim())
  if (state.value) query.set('state', state.value)
  if (from.value) query.set('from', from.value)
  if (to.value) query.set('to', to.value)
  try {
    const page = await ordersApi.list(query)
    rows.value = page.items
    total.value = page.total
    if (offset.value >= total.value && offset.value > 0) {
      offset.value = Math.max(0, Math.ceil(total.value / pageSize - 1) * pageSize)
      await loadOrders()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '订单列表读取失败。'
  } finally {
    loading.value = false
  }
}

async function openDetail(id: string) {
  selectedOrderId.value = id
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  try {
    detail.value = await ordersApi.get(id)
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '订单详情读取失败。'
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() { detail.value = null; selectedOrderId.value = null; detailError.value = '' }
function refreshOnCommit() { void loadOrders() }
function onFocus() { if (document.visibilityState === 'visible') void loadOrders() }
function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape') closeDetail() }
function previousPage() { if (offset.value > 0) { offset.value -= pageSize; void loadOrders() } }
function nextPage() { if (offset.value + pageSize < total.value) { offset.value += pageSize; void loadOrders() } }

function openTaskTrace() {
  if (!detail.value) return
  const taskId = detail.value.task.id
  closeDetail()
  void router.push({ name: 'task-workbench', params: { taskId } })
}

onMounted(() => {
  const requestedOrderId = route.query.order_id
  void loadOrders(true).then(() => {
    if (typeof requestedOrderId === 'string') void openDetail(requestedOrderId)
  })
  window.addEventListener('tradepilot:order-created', refreshOnCommit)
  window.addEventListener('focus', onFocus)
  document.addEventListener('visibilitychange', onFocus)
  document.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('tradepilot:order-created', refreshOnCommit)
  window.removeEventListener('focus', onFocus)
  document.removeEventListener('visibilitychange', onFocus)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <section class="order-center">
    <header class="page-heading">
      <div><p class="eyebrow">商贸订单 · 只读回查</p><h1>订单中心</h1><p class="order-center__subtitle">查看已人工批准并正式提交的订单，按合同、项目、类型和日期筛选。</p></div>
      <div class="page-heading__actions"><button class="secondary-button" type="button" :disabled="loading" @click="loadOrders()"><AppIcon name="refresh" :size="16" />刷新列表</button></div>
    </header>

    <form class="order-filters" @submit.prevent="loadOrders(true)">
      <label><span>订单类型</span><select v-model="type"><option value="">全部类型</option><option value="SALES">销售</option><option value="PURCHASE">采购</option></select></label>
      <label><span>合同编号</span><input v-model="contract" type="search" placeholder="输入合同编号" maxlength="64"></label>
      <label><span>工程项目</span><input v-model="project" type="search" placeholder="输入项目名称或编号" maxlength="128"></label>
      <label><span>订单状态</span><select v-model="state"><option value="">全部状态</option><option value="CREATED">已生成</option></select></label>
      <label><span>开始日期（UTC）</span><input v-model="from" type="date" :max="to || undefined"></label>
      <label><span>结束日期（UTC）</span><input v-model="to" type="date" :min="from || undefined" :max="dateToday"></label>
      <button class="primary-button order-filter-button" type="submit" :disabled="loading">查询</button>
      <button class="text-button order-clear-button" type="button" @click="type = ''; contract = ''; project = ''; state = ''; from = ''; to = ''; loadOrders(true)">清除条件</button>
    </form>

    <div v-if="errorMessage" class="notice-banner notice-banner--danger" role="alert"><strong>订单列表加载失败</strong><span>{{ errorMessage }}</span><button class="secondary-button" type="button" @click="loadOrders()">重试</button></div>
    <section class="component-table-card order-table-card" aria-label="正式订单">
      <div class="order-table-heading"><div><h2>正式订单</h2><p>仅显示当前账号授权部门内的服务端订单记录</p></div><span v-if="!loading">共 {{ total }} 条</span></div>
      <div v-if="loading" class="order-list-state" role="status"><span class="loading-spinner"></span>正在读取授权范围内的订单…</div>
      <div v-else class="data-table-wrap">
        <table class="data-table order-data-table">
          <thead><tr><th>订单编号</th><th>类型</th><th>合同 / 项目</th><th class="text-right">含税金额</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody v-if="rows.length"><tr v-for="row in rows" :key="row.id">
            <td><strong>{{ row.number }}</strong></td><td>{{ typeLabel(row.type) }}</td>
            <td><strong>{{ row.contract_number }}</strong><small>{{ row.project_code }} · {{ row.project_name }}</small></td>
            <td class="text-right">{{ formatMoney(row.total_amount, row.currency) }}</td>
            <td><StatusBadge :label="stateLabel(row.state)" tone="success" /></td><td>{{ formatTime(row.created_at) }}</td>
            <td><button class="text-button" type="button" @click="openDetail(row.id)">查看详情</button></td>
          </tr></tbody>
          <tbody v-else><tr><td colspan="7" class="data-table__empty">{{ errorMessage ? '订单暂不可用，请查看上方错误后重试。' : '当前筛选条件下没有正式订单。' }}</td></tr></tbody>
        </table>
      </div>
      <footer class="order-pagination"><span>显示 {{ total ? offset + 1 : 0 }}–{{ Math.min(offset + rows.length, total) }} 条 / 共 {{ total }} 条</span><div><button class="secondary-button" type="button" :disabled="loading || offset === 0" @click="previousPage">上一页</button><span>第 {{ currentPage }} / {{ pageCount }} 页</span><button class="secondary-button" type="button" :disabled="loading || offset + pageSize >= total" @click="nextPage">下一页</button></div></footer>
    </section>

    <div v-if="detail || detailLoading || detailError" class="order-drawer-backdrop" @click.self="closeDetail">
      <aside class="order-drawer" role="dialog" aria-modal="true" :aria-label="detail ? `${typeLabel(detail.type)}订单详情` : '订单详情'">
        <header class="order-drawer__header"><div><p class="eyebrow">正式订单详情</p><h2>{{ detail?.number ?? '订单详情' }}</h2></div><button class="icon-button" type="button" aria-label="关闭详情" @click="closeDetail">×</button></header>
        <div v-if="detailLoading" class="order-list-state"><span class="loading-spinner"></span>正在核对订单与批准快照…</div>
        <div v-else-if="detailError" class="order-drawer__body"><div class="notice-banner notice-banner--danger" role="alert"><strong>订单详情不可用</strong><span>{{ detailError }}</span><button class="secondary-button" type="button" @click="selectedOrderId && openDetail(selectedOrderId)">重试</button></div></div>
        <template v-else-if="detail">
          <div class="order-drawer__body">
            <div class="order-detail-status"><StatusBadge label="正式订单已生成" tone="success" /><StatusBadge :label="`批准草稿 v${detail.approval.version}`" tone="info" /></div>
            <div class="order-detail-grid"><div><span>合同 / 项目</span><strong>{{ detail.contract.number }} / {{ detail.contract.project_name }}</strong></div><div><span>客户</span><strong>{{ detail.contract.customer_name }}</strong></div><div><span>经办人 / 部门</span><strong>{{ detail.operator.login }} / {{ detail.department.name }}</strong></div><div><span>创建时间</span><strong>{{ formatTime(detail.created_at) }}</strong></div><div><span>交付日期</span><strong>{{ detail.delivery_date ?? '—' }}</strong></div><div><span>收货地址 / 联系人</span><strong>{{ detail.address ?? '—' }} / {{ detail.contact ?? '—' }}</strong></div></div>
            <h3>物资明细</h3><div class="data-table-wrap"><table class="data-table order-item-table"><thead><tr><th>物料 / 编码</th><th>数量</th><th>单价</th><th>金额</th></tr></thead><tbody><tr v-for="item in detail.items" :key="item.line_no"><td><strong>{{ item.raw_name ?? item.material_name }}</strong><small>{{ item.material_code }} · {{ item.specification ?? item.material_name }}</small></td><td>{{ item.quantity }} {{ item.unit }}</td><td>{{ formatMoney(item.unit_price, detail.currency) }}</td><td>{{ formatMoney(item.amount, detail.currency) }}</td></tr><tr v-if="!detail.items.length"><td colspan="4" class="data-table__empty">订单没有明细行。</td></tr></tbody></table></div>
            <div class="order-amount-summary"><strong>含税合计 {{ formatMoney(detail.total_amount, detail.currency) }}</strong><div><span><small>本单已占用</small><strong>{{ formatMoney(detail.total_amount, detail.currency) }}</strong></span><span><small>当前合同剩余额度</small><strong>{{ detail.contract.balance_available && detail.contract.remaining_amount !== null ? formatMoney(detail.contract.remaining_amount, detail.currency) : '暂不可用' }}</strong></span></div><p v-if="!detail.contract.balance_available" role="status">合同额度账当前不可用，订单记录仍保持只读展示。</p></div>
            <section class="order-audit-section"><h3>批准与操作追溯</h3><ol class="order-audit-list"><li><span class="order-audit-dot"></span><div><strong>批准当前版本</strong><small>{{ formatTime(detail.approval.created_at) }} · {{ detail.approval.approved_by }} · v{{ detail.approval.version }}</small></div></li><li v-for="(event, index) in detail.audit" :key="`${event.action}-${event.created_at}-${index}`"><span class="order-audit-dot"></span><div><strong>{{ event.action }} · {{ event.result }}</strong><small>{{ formatTime(event.created_at) }} · {{ event.actor }}<template v-if="event.input_hash"> · 输入摘要 {{ event.input_hash.slice(0, 12) }}…</template></small></div></li><li><span class="order-audit-dot"></span><div><strong>订单快照</strong><small>批准版本 v{{ detail.approval.version }} · {{ detail.approval.content_hash.slice(0, 12) }}… · 订单 {{ detail.number }}</small></div></li></ol>
              <div v-if="detail.sources.length" class="order-source-list"><strong>字段来源记录（批准版本）</strong><div v-for="source in detail.sources" :key="`${source.field_path}-${source.source_type}`"><span>{{ source.field_path }}</span><small>{{ source.source_type }}<template v-if="source.locator"> · {{ JSON.stringify(source.locator) }}</template><template v-if="source.confirmed_by"> · 确认人 {{ source.confirmed_by }}</template></small></div></div><p v-else class="order-source-empty">当前批准版本没有单独保存字段来源记录。</p>
            </section>
          </div>
          <footer class="order-drawer__footer"><button class="secondary-button" type="button" @click="openTaskTrace"><AppIcon name="file" :size="16" />查看原始输入</button><button class="primary-button" type="button" @click="openTaskTrace"><AppIcon name="trace" :size="16" />打开任务与运行轨迹</button></footer>
        </template>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.order-center__subtitle { margin: 8px 0 0; color: var(--color-muted); font-size: 13px; }
.order-filters { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); align-items: end; gap: 12px; margin-bottom: 17px; padding: 18px; border: 1px solid var(--color-border); border-radius: 7px; background: #fff; }
.order-filters label { display: grid; min-width: 0; gap: 6px; color: #617888; font-size: 11px; }
.order-filters input, .order-filters select { width: 100%; min-width: 0; height: 39px; padding: 0 10px; border: 1px solid #dce5ea; border-radius: 5px; background: #fff; color: #263e4d; font: inherit; font-size: 12px; }
.order-filter-button { min-height: 39px; padding: 0 12px; }
.order-clear-button { justify-self: center; min-height: 39px; }
.order-table-card { margin-top: 0; padding: 19px; }
.order-table-heading, .order-pagination, .order-pagination > div { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.order-table-heading { margin-bottom: 14px; }
.order-table-heading h2 { margin: 0; font-size: 15px; }
.order-table-heading p, .order-table-heading > span, .order-pagination { margin: 4px 0 0; color: var(--color-muted); font-size: 11px; }
.order-data-table th, .order-data-table td { min-width: 90px; }
.order-data-table th:first-child, .order-data-table td:first-child { min-width: 180px; }
.order-data-table td strong, .order-data-table td small, .order-item-table td strong, .order-item-table td small { display: block; }
.order-data-table td small, .order-item-table td small { margin-top: 4px; color: var(--color-muted); font-size: 10px; }
.order-data-table td:last-child { min-width: 88px; }
.order-pagination { margin-top: 14px; }
.order-pagination > div { margin-left: auto; }
.order-pagination .secondary-button { min-height: 32px; padding: 0 9px; font-size: 11px; }
.order-list-state { display: flex; min-height: 170px; align-items: center; justify-content: center; gap: 10px; color: var(--color-muted); font-size: 13px; }
.loading-spinner { width: 17px; height: 17px; border: 2px solid #d8e5e9; border-top-color: var(--color-primary); border-radius: 50%; animation: order-spin .75s linear infinite; }
.order-drawer-backdrop { position: fixed; z-index: 30; inset: 0; display: flex; justify-content: flex-end; background: rgb(11 28 39 / 38%); }
.order-drawer { display: flex; width: min(760px, 94vw); height: 100%; flex-direction: column; background: #fff; box-shadow: -12px 0 35px rgb(12 38 52 / 14%); animation: order-drawer-in .18s ease-out; }
.order-drawer__header { display: flex; min-height: 76px; align-items: center; justify-content: space-between; padding: 10px 24px; border-bottom: 1px solid #e2e9ee; }
.order-drawer__header h2 { margin: 2px 0 0; font-size: 17px; }
.order-drawer__body { overflow: auto; flex: 1; padding: 21px 24px; color: #324a59; font-size: 12px; line-height: 1.65; }
.order-detail-status { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.order-detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; padding-bottom: 16px; border-bottom: 1px solid #e2e9ee; }
.order-detail-grid > div { display: grid; gap: 4px; min-width: 0; }
.order-detail-grid span, .order-amount-summary small { color: var(--color-muted); font-size: 10px; }
.order-detail-grid strong { overflow-wrap: anywhere; color: #263e4d; font-weight: 550; }
.order-drawer__body h3 { margin: 19px 0 9px; font-size: 14px; }
.order-item-table th, .order-item-table td { min-width: 75px; padding: 9px; font-size: 11px; }
.order-item-table td:first-child { min-width: 180px; }
.order-amount-summary { margin-top: 14px; padding: 13px; background: #f4f7f9; }
.order-amount-summary > strong { display: block; margin: 0 0 13px; color: #203c4d; font-size: 18px; text-align: right; }
.order-amount-summary > div { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.order-amount-summary > div span { display: grid; gap: 3px; }
.order-amount-summary > div strong { color: var(--color-primary); font-size: 17px; }
.order-amount-summary p { margin: 9px 0 0; color: var(--color-warning); font-size: 11px; }
.order-audit-section { margin-top: 18px; }
.order-audit-section h3 { margin-top: 0 !important; }
.order-audit-list { display: grid; gap: 11px; margin: 0; padding: 0; list-style: none; }
.order-audit-list li { display: flex; align-items: flex-start; gap: 9px; }
.order-audit-dot { width: 9px; height: 9px; flex: 0 0 auto; margin: 5px 0 0 1px; border: 2px solid #2c956c; border-radius: 50%; }
.order-audit-list li div { display: grid; gap: 3px; }
.order-audit-list strong { color: #3d5969; font-size: 11px; font-weight: 550; }
.order-audit-list small, .order-source-list small { color: var(--color-muted); font-size: 10px; }
.order-source-list { display: grid; gap: 7px; margin-top: 15px; padding: 12px; border: 1px solid #e4ebef; border-radius: 5px; }
.order-source-list > strong { font-size: 11px; }
.order-source-list > div { display: grid; grid-template-columns: minmax(100px, .6fr) minmax(0, 1.4fr); gap: 10px; }
.order-source-list > div > span { overflow-wrap: anywhere; color: #476273; font-size: 10px; }
.order-source-empty { margin: 11px 0 0; color: var(--color-muted); font-size: 10px; }
.order-drawer__footer { display: flex; justify-content: space-between; gap: 10px; padding: 14px 24px; border-top: 1px solid #e2e9ee; }
.order-drawer__footer button { min-height: 37px; padding: 0 11px; font-size: 11px; }
@keyframes order-spin { to { transform: rotate(360deg); } }
@keyframes order-drawer-in { from { transform: translateX(15px); opacity: .75; } to { transform: translateX(0); opacity: 1; } }
@media (max-width: 1050px) { .order-filters { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 700px) { .order-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 13px; } .order-table-card { padding: 13px; } .order-pagination { align-items: flex-start; flex-direction: column; } .order-pagination > div { margin-left: 0; } .order-drawer__header { padding: 9px 16px; } .order-drawer__body { padding: 16px; } .order-detail-grid { grid-template-columns: 1fr; } .order-drawer__footer { padding: 12px 15px; } .order-drawer__footer button { flex: 1; } }
</style>
