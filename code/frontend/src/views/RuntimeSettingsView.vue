<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import NoticeBanner from '../components/NoticeBanner.vue'
import { adminConfigApi, type RuntimeConfig } from '../services/adminConfig'

const busy = ref(false)
const error = ref('')
const saved = ref('')
const version = ref(0)
const versions = ref<Array<{ version: number; created_by: string; created_at: string; is_current: boolean; content_hash: string }>>([])
const keyStatus = reactive({ model: false, ocr: false })
const modelKeyInput = ref('')
const ocrKeyInput = ref('')
const secretStorageAvailable = ref(false)
const form = reactive<RuntimeConfig['content']>({
  model_provider: null, model_name: null, model_base_url: null, model_timeout_seconds: 60,
  model_max_retries: 2, model_max_rounds: 8, model_max_tool_calls: 20, max_concurrency: 5,
  ocr_provider: null, mcp_url: 'http://mcp:8001/mcp', business_timezone: 'Asia/Hong_Kong',
  trace_retention_days: 30, artifact_retention_days: 30, approval_mode: 'human_required',
})

async function load() {
  busy.value = true
  error.value = ''
  try {
    const [result, history] = await Promise.all([adminConfigApi.config(), adminConfigApi.versions()])
    version.value = result.version
    Object.assign(form, result.content)
    keyStatus.model = result.model_api_key_configured
    keyStatus.ocr = result.ocr_api_key_configured
    secretStorageAvailable.value = result.secret_storage_available
    versions.value = history
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取配置失败。' }
  finally { busy.value = false }
}

async function save() {
  busy.value = true
  error.value = ''
  saved.value = ''
  try {
    const result = await adminConfigApi.update({
      ...form,
      expected_version: version.value,
      ...(modelKeyInput.value.trim() ? { model_api_key: modelKeyInput.value.trim() } : {}),
      ...(ocrKeyInput.value.trim() ? { ocr_api_key: ocrKeyInput.value.trim() } : {}),
    })
    modelKeyInput.value = ''
    ocrKeyInput.value = ''
    version.value = result.version
    Object.assign(form, result.content)
    saved.value = `配置 v${result.version} 已保存；只对之后创建的任务生效。`
    await load()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存配置失败。' }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="runtime-settings">
    <header class="page-heading">
      <div><h1>运行参数与业务规则</h1><p>配置运行参数、工具服务和审批模式；密钥由部署环境提供，只显示配置状态。</p></div>
      <div class="page-heading__actions">
        <RouterLink class="secondary-button" :to="{ name: 'audit-log' }">系统操作审计</RouterLink>
        <button class="primary-button" type="button" :disabled="busy" @click="save">{{ busy ? '处理中…' : '保存配置' }}</button>
      </div>
    </header>
    <NoticeBanner v-if="error" tone="danger" title="配置操作失败" :message="error" />
    <NoticeBanner v-if="saved" tone="success" title="保存成功" :message="saved" />
    <div class="runtime-settings__grid">
      <form class="surface-card runtime-settings__form" @submit.prevent="save">
        <h2>模型与文档解析</h2>
        <div class="runtime-settings__fields">
          <label>模型供应商<select v-model="form.model_provider"><option :value="null">未配置</option><option value="openai">OpenAI</option><option value="deepseek">DeepSeek</option></select></label>
          <label>模型标识<input v-model="form.model_name" maxlength="128" placeholder="例如 gpt-5.6-luna" /></label>
          <label>模型 API 密钥（留空保持不变）<input v-model="modelKeyInput" type="password" autocomplete="new-password" :placeholder="keyStatus.model ? '已设置；不会回显' : '尚未配置'" /></label>
          <label>OCR API 密钥（留空保持不变）<input v-model="ocrKeyInput" type="password" autocomplete="new-password" :placeholder="keyStatus.ocr ? '已设置；不会回显' : '尚未配置'" /></label>
          <label class="runtime-settings__wide">模型服务地址<input v-model="form.model_base_url" placeholder="留空使用供应商默认地址" /></label>
          <label>调用超时（秒）<input v-model.number="form.model_timeout_seconds" type="number" min="1" max="300" step="1" required /></label>
          <label>临时故障重试次数<input v-model.number="form.model_max_retries" type="number" min="0" max="5" required /></label>
          <label>模型轮次上限<input v-model.number="form.model_max_rounds" type="number" min="1" max="50" required /></label>
          <label>工具调用上限<input v-model.number="form.model_max_tool_calls" type="number" min="1" max="100" required /></label>
          <label>并发任务上限<input v-model.number="form.max_concurrency" type="number" min="1" max="5" required /></label>
          <label>OCR 适配器<select v-model="form.ocr_provider"><option :value="null">未配置</option><option value="local_tesseract">本地 Tesseract</option><option value="deepseek">DeepSeek</option><option value="openai">OpenAI</option></select></label>
        </div>
        <h2>MCP 工具与业务规则</h2>
        <div class="runtime-settings__fields">
          <label class="runtime-settings__wide">MCP 服务地址<input v-model="form.mcp_url" type="url" required /></label>
          <label>业务时区<input v-model="form.business_timezone" required /></label>
          <label>审批模式<select v-model="form.approval_mode"><option value="human_required">必须由其他授权用户批准</option><option value="demo_self_approval">演示模式：允许本人批准</option></select></label>
          <label>轨迹留存（天）<input v-model.number="form.trace_retention_days" type="number" min="1" max="3650" required /></label>
          <label>附件留存（天）<input v-model.number="form.artifact_retention_days" type="number" min="1" max="3650" required /></label>
        </div>
        <div class="notice-banner notice-banner--warning runtime-settings__notice"><span class="notice-banner__icon">!</span><div><strong>安全约束</strong><p>超额始终硬阻断，正式订单始终要求有效批准。演示本人批准仅在服务端显式开启演示模式时可用。模型密钥 {{ keyStatus.model ? '已配置' : '尚未配置' }}，OCR 密钥 {{ keyStatus.ocr ? '已配置' : '尚未配置' }}；服务端凭据加密存储{{ secretStorageAvailable ? '已就绪' : '未配置，请先设置 RUNTIME_CONFIG_ENCRYPTION_KEY' }}。密钥写入后不会再次回显。</p></div></div>
        <p class="runtime-settings__footnote">当前版本 v{{ version }}。修改只影响之后创建的任务；既有任务保留原配置版本。清理会保护活动任务与正式订单证据。</p>
      </form>
      <aside class="surface-card runtime-settings__history">
        <h2>最近配置版本</h2>
        <div v-if="!versions.length" class="runtime-settings__empty">尚无管理员配置版本，当前使用环境默认值。</div>
        <div v-for="item in versions" :key="item.version" class="runtime-settings__version">
          <div><strong>版本 v{{ item.version }}</strong><span v-if="item.is_current" class="runtime-settings__current">当前</span></div>
          <small>{{ item.created_by }} · {{ new Date(item.created_at).toLocaleString('zh-CN') }}</small>
          <code>{{ item.content_hash.slice(0, 16) }}…</code>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.runtime-settings { display: grid; gap: 18px; }
.runtime-settings__grid { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(260px, .8fr); align-items: start; gap: 18px; }
.runtime-settings__form, .runtime-settings__history { padding: 22px; border: 1px solid var(--color-border); border-radius: 8px; background: #fff; }
.runtime-settings h2 { margin: 0 0 16px; font-size: var(--font-size-md); }
.runtime-settings h2:not(:first-child) { margin-top: 26px; }
.runtime-settings__fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.runtime-settings__fields label { display: grid; gap: 6px; color: var(--color-muted); font-size: var(--font-size-sm); }
.runtime-settings__fields input, .runtime-settings__fields select { min-width: 0; height: 40px; border: 1px solid var(--color-border); border-radius: 5px; padding: 0 10px; color: #172b3a; background: white; }
.runtime-settings__wide { grid-column: 1 / -1; }
.runtime-settings__notice { margin-top: 22px; }
.runtime-settings__footnote, .runtime-settings__empty { color: var(--color-muted); font-size: var(--font-size-sm); line-height: 1.6; }
.runtime-settings__version { display: grid; gap: 7px; padding: 14px 0; border-bottom: 1px solid var(--color-border); font-size: var(--font-size-sm); }
.runtime-settings__version div { display: flex; justify-content: space-between; }
.runtime-settings__version small { color: var(--color-muted); }
.runtime-settings__version code { color: var(--color-muted); font-size: var(--font-size-sm); }
.runtime-settings__current { color: var(--color-success); }
@media (max-width: 900px) { .runtime-settings__grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .runtime-settings__fields { grid-template-columns: 1fr; } .runtime-settings__wide { grid-column: auto; } }
</style>
