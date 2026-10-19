<script setup lang="ts">
import { ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import { useUiStore } from '../stores/ui'
import { importsApi, type ImportPreview } from '../services/imports'

const ui = useUiStore()
const orderType = ref<'销售订单' | '采购订单'>('销售订单')
const inputMode = ref<'自然语言' | 'PDF / 单据图片' | 'Excel 批量'>('自然语言')
const requestText = ref('')
const excelInput = ref<HTMLInputElement | null>(null)
const excelLoading = ref(false)
const excelError = ref('')
const excelPreview = ref<ImportPreview | null>(null)

const modes = ['自然语言', 'PDF / 单据图片', 'Excel 批量'] as const

function startTask() {
  ui.notify('任务创建接口将在 M2 接入。本次没有提交文本、上传文件或创建任务。')
}

function continueTask() {
  ui.notify('任务恢复接口将在 M2 接入；目前没有读取或生成任务记录。')
}

async function downloadExcelTemplate() {
  try {
    const blob = await importsApi.downloadTemplate()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'TradePilot-订单模板.xlsx'
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    excelError.value = error instanceof Error ? error.message : '模板下载失败。'
  }
}

async function previewExcel(event: Event) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  excelLoading.value = true
  excelError.value = ''
  excelPreview.value = null
  try {
    excelPreview.value = await importsApi.preview(file)
  } catch (error) {
    excelError.value = error instanceof Error ? error.message : 'Excel 解析失败。'
  } finally {
    excelLoading.value = false
    target.value = ''
  }
}
</script>

<template>
  <section class="page-heading">
    <div>
      <p class="eyebrow">工作空间 / 智能提单</p>
      <h1>开始一笔新订单</h1>
      <p class="page-description">选择业务方向与输入方式，后续工作流接入后将补齐字段并生成待确认草稿。</p>
    </div>
    <div class="page-heading__actions">
      <button class="secondary-button" type="button" @click="continueTask"><AppIcon name="history" :size="17" />继续上次任务</button>
      <RouterLink class="secondary-button" to="/interface-states"><AppIcon name="info" :size="17" />界面状态</RouterLink>
    </div>
  </section>

  <div class="order-shortcuts">
    <div class="segmented-control" role="group" aria-label="订单类型">
      <button type="button" :class="{ selected: orderType === '销售订单' }" @click="orderType = '销售订单'"><AppIcon name="plus" :size="16" />新建销售订单</button>
      <button type="button" :class="{ selected: orderType === '采购订单' }" @click="orderType = '采购订单'"><AppIcon name="plus" :size="16" />新建采购订单</button>
    </div>
    <span class="scope-hint">当前身份和部门范围由登录会话确定</span>
  </div>

  <div class="workbench-grid">
    <section class="card input-card">
      <div class="mode-tabs" role="tablist" aria-label="输入方式">
        <button v-for="mode in modes" :key="mode" type="button" role="tab" :aria-selected="inputMode === mode" :class="{ selected: inputMode === mode }" @click="inputMode = mode">{{ mode }}</button>
      </div>

      <template v-if="inputMode === '自然语言'">
        <label class="sr-only" for="order-description">{{ orderType }}业务描述</label>
        <textarea id="order-description" v-model="requestText" class="order-description" :placeholder="`请描述${orderType}需求，例如合同、项目、物料、规格、数量和交期。`" />
        <div class="input-card__footer">
          <span>身份与部门范围来自当前登录会话，不接受文本或附件修改授权。</span>
          <button class="primary-button" type="button" @click="startTask"><AppIcon name="arrow" :size="17" />开始解析</button>
        </div>
      </template>

      <div v-else-if="inputMode === 'Excel 批量'" class="excel-import-panel">
        <div class="excel-import-panel__intro"><AppIcon name="grid" :size="27" /><div><h2>Excel 订单分组与校验</h2><p>下载模板后上传，系统按分组预览订单明细并标出单元格问题。</p></div></div>
        <div class="excel-import-panel__actions"><button class="secondary-button" type="button" @click="downloadExcelTemplate"><AppIcon name="file" :size="16" />下载订单模板</button><button class="primary-button" type="button" :disabled="excelLoading" @click="excelInput?.click()"><AppIcon name="upload" :size="16" />{{ excelLoading ? '正在解析…' : '选择 Excel 文件' }}</button><input ref="excelInput" class="sr-only" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" @change="previewExcel" /></div>
        <NoticeBanner v-if="excelError" tone="warning" :message="excelError" />
        <p class="excel-import-panel__hint">仅支持 XLSX，最多 10 MB、500 条明细；公式字段会被拒绝。当前只进行分组预览和校验，不创建任务或订单。</p>
        <div v-if="excelPreview" class="excel-preview">
          <div class="excel-preview__summary"><strong>{{ excelPreview.summary.detail_rows }} 条明细 · {{ excelPreview.summary.group_count }} 个订单组</strong><span>{{ excelPreview.summary.ready_groups }} 组结构完整 · {{ excelPreview.summary.over_quota_groups }} 组超额待核验 · {{ excelPreview.summary.needs_correction_groups }} 组待修正</span></div>
          <article v-for="group in excelPreview.groups" :key="`${group.sheet}:${group.group_id}`" class="excel-preview__group"><header><strong>{{ group.group_id }}</strong><span>工作表 {{ group.sheet }} · 第 {{ group.rows.join('、') }} 行</span><b :class="group.status === 'NEEDS_CORRECTION' || group.status === 'OVER_QUOTA_REVIEW' ? 'is-warning' : 'is-ready'">{{ group.status === 'NEEDS_CORRECTION' ? '待修正' : group.status === 'OVER_QUOTA_REVIEW' ? '超额待核验' : '结构完整，待确认' }}</b></header><p>候选金额：{{ group.total_amount ?? '待完善' }} 元 · 合并 {{ group.candidate.items.length }} 条明细</p><p v-if="group.quota">合同剩余额度：{{ group.quota.remaining_before_group }} 元</p><ul v-if="group.issues.length"><li v-for="(issue, index) in group.issues" :key="`${issue.locator.cell}:${issue.code}:${index}`"><span>{{ issue.locator.cell || `${issue.locator.sheet} 表头` }}</span>{{ issue.message }}</li></ul><p v-else>候选草稿仍需业务确认和服务端校验，不会直接提交。</p></article>
          <ul v-if="excelPreview.issues.length" class="excel-preview__issues"><li v-for="(issue, index) in excelPreview.issues" :key="`${issue.locator.cell}:${issue.code}:${index}`">{{ issue.locator.cell || `${issue.locator.sheet} 表头` }}：{{ issue.message }}</li></ul>
        </div>
      </div>
      <div v-else class="not-connected-panel">
        <AppIcon name="upload" :size="27" />
        <h2>{{ inputMode }}入口预留</h2>
        <p>文件解析与批量任务将在后续阶段接入。当前不会读取或上传文件。</p>
        <button class="secondary-button" type="button" @click="ui.notify('当前未接入文件上传，未选择或上传任何文件。')">查看接入状态</button>
      </div>

      <div class="upload-tiles">
        <article class="upload-tile">
          <AppIcon name="file" :size="24" />
          <h3>上传 PDF / 单据图片</h3>
          <p>文件接入将在后续开发，不会在此页面模拟解析结果。</p>
          <button class="secondary-button" type="button" @click="ui.notify('附件上传尚未接入，当前没有文件被读取。')"><AppIcon name="upload" :size="16" />选择文件</button>
        </article>
        <article class="upload-tile">
          <AppIcon name="grid" :size="24" />
          <h3>导入 Excel 订单</h3>
          <p>下载模板并预览订单分组、字段冲突与异常单元格。</p>
          <button class="secondary-button" type="button" @click="inputMode = 'Excel 批量'">开始导入</button>
        </article>
      </div>
      <NoticeBanner tone="info" message="自然语言任务创建仍在开发；Excel 当前提供只读分组预览，不会生成草稿、任务或正式订单。" />
    </section>

    <aside class="card howto-card">
      <h2>如何完成提单</h2>
      <ol class="workflow-steps">
        <li><strong><span>01</span>描述业务需求</strong><p>合同、项目、物料、规格、数量、单价与交期。</p></li>
        <li><strong><span>02</span>补齐并核验</strong><p>物料多候选需确认，价格与身份有明确来源。</p></li>
        <li><strong><span>03</span>核对草稿并送审</strong><p>预览区可直接修改，保存后重新执行校验。</p></li>
        <li><strong><span>04</span>人工批准后提交</strong><p>批准绑定版本；实时额度变化可能阻止提交。</p></li>
      </ol>
      <div class="file-guidance">
        <h3>输入方式</h3>
        <p>自然语言与 PDF、图片入口仍在开发中；Excel 已支持模板下载、订单分组预览和单元格错误定位。</p>
      </div>
    </aside>
  </div>
</template>
