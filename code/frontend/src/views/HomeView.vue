<script setup lang="ts">
import { ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import { useUiStore } from '../stores/ui'

const ui = useUiStore()
const orderType = ref<'销售订单' | '采购订单'>('销售订单')
const inputMode = ref<'自然语言' | 'PDF / 单据图片' | 'Excel 批量'>('自然语言')
const requestText = ref('')

const modes = ['自然语言', 'PDF / 单据图片', 'Excel 批量'] as const

function startTask() {
  ui.notify('任务创建接口将在 M2 接入。本次没有提交文本、上传文件或创建任务。')
}

function continueTask() {
  ui.notify('任务恢复接口将在 M2 接入；目前没有读取或生成任务记录。')
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

      <div v-else class="not-connected-panel">
        <AppIcon :name="inputMode === 'Excel 批量' ? 'grid' : 'upload'" :size="27" />
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
          <p>批次、行级校验与异常修正将在后续阶段接入。</p>
          <button class="secondary-button" type="button" @click="ui.notify('Excel 批量导入尚未接入，当前没有文件被读取。')">查看说明</button>
        </article>
      </div>
      <NoticeBanner tone="info" message="当前页面仅提供已授权工作空间的入口；任务服务尚未接入，不会生成草稿或正式订单。" />
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
        <p>自然语言入口已展示；PDF、图片和 Excel 上传能力尚未接入。当前页面不会模拟文件内容或解析结果。</p>
      </div>
    </aside>
  </div>
</template>
