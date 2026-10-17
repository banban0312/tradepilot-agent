<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import NoticeBanner from '../components/NoticeBanner.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { masterDataApi } from '../services/masterData'
import type { ContractRecord, Department, MaterialLookup, MaterialRecord } from '../services/masterData'
import { useUiStore } from '../stores/ui'

type EditableLine = {
  line_id: string | null
  material_id: string
  material_code: string
  material_name: string
  specification: string
  unit: string
  unit_price: string
  max_quantity: string
}

const ui = useUiStore()
const activeTab = ref<'contracts' | 'materials'>('contracts')
const loading = ref(false)
const saving = ref(false)
const pageError = ref('')
const departments = ref<Department[]>([])
const selectedDepartmentId = ref('')
const contractRows = ref<ContractRecord[]>([])
const contractQuery = ref('')
const contractType = ref('')
const contractStatus = ref('')
const selectedContractId = ref('')
const selectedContract = ref<ContractRecord | null>(null)
const contractMode = ref<'view' | 'create'>('view')
const lookups = ref<{ customers: Array<{ id: string; code: string; name: string }>; projects: Array<{ id: string; code: string; name: string }>; materials: MaterialLookup[] }>({ customers: [], projects: [], materials: [] })
const contractForm = reactive({
  department_id: '',
  number: '',
  type: 'SALES',
  customer_id: '',
  project_id: '',
  currency: 'CNY',
  total_amount: '',
  status: 'ACTIVE',
  valid_from: '',
  valid_to: '',
  expected_version: 1,
  lines: [] as EditableLine[],
})

const materialRows = ref<MaterialRecord[]>([])
const materialQuery = ref('')
const materialActiveFilter = ref('')
const selectedMaterialId = ref('')
const selectedMaterial = ref<MaterialRecord | null>(null)
const materialMode = ref<'view' | 'create'>('view')
const materialForm = reactive({
  code: '',
  name: '',
  specification: '',
  unit: '',
  active: true,
  aliases_text: '',
  expected_version: 1,
})
const previewName = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const preview = ref<{ candidates: Array<{ material_id: string; code: string; name: string; specification: string; unit: string; reason: string; requires_confirmation: boolean }> } | null>(null)

const isCreatingContract = computed(() => contractMode.value === 'create')
const isCreatingMaterial = computed(() => materialMode.value === 'create')

function money(value: string | null | undefined) {
  if (!value) return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 2 }).format(number)
    : value
}

function showError(error: unknown) {
  pageError.value = error instanceof Error ? error.message : '操作失败，请稍后重试。'
}

async function loadDepartments() {
  departments.value = await masterDataApi.departments()
  if (!selectedDepartmentId.value && departments.value.length) selectedDepartmentId.value = departments.value[0].id
}

async function loadLookups(departmentId: string) {
  if (!departmentId) return
  lookups.value = await masterDataApi.lookups(departmentId)
}

async function loadContracts() {
  loading.value = true
  pageError.value = ''
  try {
    const filters = new URLSearchParams()
    if (contractQuery.value.trim()) filters.set('q', contractQuery.value.trim())
    if (selectedDepartmentId.value) filters.set('department_id', selectedDepartmentId.value)
    if (contractType.value) filters.set('type', contractType.value)
    if (contractStatus.value) filters.set('status', contractStatus.value)
    const result = await masterDataApi.contracts(filters)
    contractRows.value = result.items
    if (selectedContractId.value && result.items.some((item) => item.id === selectedContractId.value)) {
      await openContract(selectedContractId.value)
    } else if (contractRows.value.length && contractMode.value === 'view') {
      await openContract(contractRows.value[0].id)
    } else if (!contractRows.value.length && contractMode.value === 'view') {
      selectedContract.value = null
      selectedContractId.value = ''
    }
  } catch (error) {
    showError(error)
  } finally {
    loading.value = false
  }
}

async function openContract(id: string) {
  pageError.value = ''
  try {
    selectedContractId.value = id
    selectedContract.value = await masterDataApi.contract(id)
    contractMode.value = 'view'
    await loadLookups(selectedContract.value.department_id)
    contractForm.department_id = selectedContract.value.department_id
    contractForm.number = selectedContract.value.number
    contractForm.type = selectedContract.value.type
    contractForm.customer_id = selectedContract.value.customer_id
    contractForm.project_id = selectedContract.value.project_id
    contractForm.currency = selectedContract.value.currency
    contractForm.total_amount = selectedContract.value.total_amount
    contractForm.status = selectedContract.value.status
    contractForm.valid_from = selectedContract.value.valid_from
    contractForm.valid_to = selectedContract.value.valid_to
    contractForm.expected_version = selectedContract.value.version
    contractForm.lines = selectedContract.value.lines.map((line) => ({
      line_id: line.id,
      material_id: line.material_id,
      material_code: line.material_code,
      material_name: line.material_name,
      specification: line.specification,
      unit: line.unit,
      unit_price: line.unit_price ?? '',
      max_quantity: line.max_quantity ?? '',
    }))
  } catch (error) {
    showError(error)
  }
}

async function startNewContract() {
  pageError.value = ''
  selectedContractId.value = ''
  selectedContract.value = null
  contractMode.value = 'create'
  contractForm.department_id = selectedDepartmentId.value || departments.value[0]?.id || ''
  contractForm.number = ''
  contractForm.type = 'SALES'
  contractForm.customer_id = ''
  contractForm.project_id = ''
  contractForm.currency = 'CNY'
  contractForm.total_amount = ''
  contractForm.status = 'ACTIVE'
  contractForm.valid_from = new Date().toISOString().slice(0, 10)
  contractForm.valid_to = ''
  contractForm.expected_version = 1
  contractForm.lines = []
  await loadLookups(contractForm.department_id)
}

function addContractLine() {
  contractForm.lines.push({ line_id: null, material_id: '', material_code: '', material_name: '', specification: '', unit: '', unit_price: '', max_quantity: '' })
}

function updateLineMaterial(line: EditableLine) {
  const item = lookups.value.materials.find((material) => material.id === line.material_id)
  line.material_code = item?.code ?? ''
  line.material_name = item?.name ?? ''
  line.specification = item?.specification ?? ''
  line.unit = item?.unit ?? ''
}

function removeNewLine(index: number) {
  if (contractForm.lines[index]?.line_id === null) contractForm.lines.splice(index, 1)
}

function contractPayload() {
  return {
    department_id: contractForm.department_id,
    number: contractForm.number.trim(),
    type: contractForm.type,
    customer_id: contractForm.customer_id,
    project_id: contractForm.project_id,
    currency: contractForm.currency,
    total_amount: contractForm.total_amount,
    status: contractForm.status,
    valid_from: contractForm.valid_from,
    valid_to: contractForm.valid_to,
    lines: contractForm.lines.map((line) => ({
      line_id: line.line_id,
      material_id: line.material_id,
      unit: line.unit,
      unit_price: line.unit_price.trim() || null,
      max_quantity: line.max_quantity.trim() || null,
    })),
  }
}

async function saveContract() {
  saving.value = true
  pageError.value = ''
  try {
    const payload = contractPayload()
    const saved = isCreatingContract.value
      ? await masterDataApi.createContract(payload)
      : await masterDataApi.updateContract(selectedContractId.value, { ...payload, expected_version: contractForm.expected_version })
    selectedContractId.value = saved.id
    contractMode.value = 'view'
    ui.notify('合同资料已保存，变更已记录。')
    await loadContracts()
    await openContract(saved.id)
  } catch (error) {
    showError(error)
  } finally {
    saving.value = false
  }
}

async function loadMaterials() {
  loading.value = true
  pageError.value = ''
  try {
    const filters = new URLSearchParams()
    if (materialQuery.value.trim()) filters.set('q', materialQuery.value.trim())
    if (materialActiveFilter.value) filters.set('active', materialActiveFilter.value)
    const result = await masterDataApi.materials(filters)
    materialRows.value = result.items
    if (selectedMaterialId.value && result.items.some((item) => item.id === selectedMaterialId.value)) {
      await openMaterial(selectedMaterialId.value)
    } else if (materialRows.value.length && materialMode.value === 'view') {
      await openMaterial(materialRows.value[0].id)
    } else if (!materialRows.value.length && materialMode.value === 'view') {
      selectedMaterial.value = null
      selectedMaterialId.value = ''
    }
  } catch (error) {
    showError(error)
  } finally {
    loading.value = false
  }
}

function fillMaterialForm(material: MaterialRecord | null) {
  materialForm.code = material?.code ?? ''
  materialForm.name = material?.name ?? ''
  materialForm.specification = material?.specification ?? ''
  materialForm.unit = material?.unit ?? ''
  materialForm.active = material?.active ?? true
  materialForm.aliases_text = material?.aliases.join('\n') ?? ''
  materialForm.expected_version = material?.version ?? 1
}

async function openMaterial(id: string) {
  const material = materialRows.value.find((item) => item.id === id)
  if (!material) return
  selectedMaterialId.value = id
  selectedMaterial.value = material
  materialMode.value = 'view'
  fillMaterialForm(material)
}

function startNewMaterial() {
  selectedMaterialId.value = ''
  selectedMaterial.value = null
  materialMode.value = 'create'
  fillMaterialForm(null)
}

function aliasesFromInput() {
  return materialForm.aliases_text
    .split(/[\n,，;；]+/)
    .map((alias) => alias.trim())
    .filter(Boolean)
}

async function saveMaterial() {
  saving.value = true
  pageError.value = ''
  try {
    const payload = {
      code: materialForm.code.trim(),
      name: materialForm.name.trim(),
      specification: materialForm.specification.trim(),
      unit: materialForm.unit.trim(),
      active: materialForm.active,
      aliases: aliasesFromInput(),
    }
    const saved = isCreatingMaterial.value
      ? await masterDataApi.createMaterial(payload)
      : await masterDataApi.updateMaterial(selectedMaterialId.value, { ...payload, expected_version: materialForm.expected_version })
    selectedMaterialId.value = saved.id
    materialMode.value = 'view'
    ui.notify('物料资料已保存，变更已记录。')
    await loadMaterials()
    await openMaterial(saved.id)
  } catch (error) {
    showError(error)
  } finally {
    saving.value = false
  }
}

async function previewMaterialMatch() {
  if (!previewName.value.trim()) return
  previewLoading.value = true
  previewError.value = ''
  try {
    preview.value = await masterDataApi.previewMatch(previewName.value.trim())
  } catch (error) {
    previewError.value = error instanceof Error ? error.message : '匹配预览失败。'
  } finally {
    previewLoading.value = false
  }
}

watch(selectedDepartmentId, async (departmentId, oldValue) => {
  if (!departmentId || departmentId === oldValue) return
  if (contractMode.value === 'create') {
    contractForm.department_id = departmentId
    await loadLookups(departmentId)
  }
  if (activeTab.value === 'contracts') await loadContracts()
})

onMounted(async () => {
  try {
    await loadDepartments()
    await Promise.all([loadContracts(), loadMaterials()])
  } catch (error) {
    showError(error)
  }
})
</script>

<template>
  <section class="master-page">
    <header class="master-page__heading">
      <div>
        <p class="eyebrow">工作空间 / 主数据与系统配置</p>
        <h1>主数据与系统配置</h1>
        <p class="master-page__subtitle">维护权威业务数据；模型提取结果不能直接修改合同额度</p>
      </div>
      <button v-if="activeTab === 'contracts'" type="button" class="primary-button" @click="startNewContract">＋ 新增合同</button>
      <button v-else type="button" class="primary-button" @click="startNewMaterial">＋ 新增标准物料</button>
    </header>

    <div class="master-tabs" role="tablist" aria-label="主数据类别">
      <button type="button" role="tab" :aria-selected="activeTab === 'contracts'" :class="{ active: activeTab === 'contracts' }" @click="activeTab = 'contracts'">合同台账</button>
      <button type="button" role="tab" :aria-selected="activeTab === 'materials'" :class="{ active: activeTab === 'materials' }" @click="activeTab = 'materials'">物料与别名</button>
      <span class="master-tabs__future">客户与项目 · 模型与工具 · 规则与审计</span>
    </div>

    <NoticeBanner v-if="pageError" title="操作未完成" :message="pageError" tone="danger" class="master-page__notice" />

    <template v-if="activeTab === 'contracts'">
      <form class="master-filters" @submit.prevent="loadContracts">
        <label>
          <span>业务方向</span>
          <select v-model="contractType"><option value="">全部方向</option><option value="SALES">销售合同</option><option value="PURCHASE">采购合同</option></select>
        </label>
        <label class="master-filters__wide">
          <span>合同 / 签约单位</span>
          <input v-model="contractQuery" placeholder="合同编号或单位名称" />
        </label>
        <label>
          <span>所属部门</span>
          <select v-model="selectedDepartmentId"><option v-for="department in departments" :key="department.id" :value="department.id">{{ department.name }}</option></select>
        </label>
        <label>
          <span>合同状态</span>
          <select v-model="contractStatus"><option value="">全部状态</option><option value="ACTIVE">有效</option><option value="INACTIVE">已停用</option></select>
        </label>
        <button type="submit" class="secondary-button" :disabled="loading">⌕ 查询</button>
      </form>

      <div class="master-workspace">
        <section class="master-card master-card--ledger">
          <div class="master-card__title"><h2>合同台账</h2><span>{{ contractRows.length }} 项</span></div>
          <div class="master-table-wrap">
            <table class="master-table">
              <thead><tr><th>合同 / 签约单位</th><th class="numeric">合同总额</th><th class="numeric">剩余可用</th><th>状态</th><th>操作</th></tr></thead>
              <tbody v-if="contractRows.length">
                <tr v-for="contract in contractRows" :key="contract.id" :class="{ selected: selectedContractId === contract.id && !isCreatingContract }" @click="openContract(contract.id)">
                  <td><strong>{{ contract.number }}</strong><small>{{ contract.customer_name }} · {{ contract.project_name }}</small></td>
                  <td class="numeric">{{ money(contract.total_amount) }}</td>
                  <td class="numeric">{{ money(contract.remaining_amount) }}</td>
                  <td><StatusBadge :label="contract.status === 'ACTIVE' ? '有效' : '已停用'" :tone="contract.status === 'ACTIVE' ? 'success' : 'info'" /></td>
                  <td><button class="text-link" type="button" @click.stop="openContract(contract.id)">编辑</button></td>
                </tr>
              </tbody>
              <tbody v-else><tr><td colspan="5" class="master-table__empty">{{ loading ? '正在读取合同…' : '没有符合条件的合同' }}</td></tr></tbody>
            </table>
          </div>
          <NoticeBanner title="余额只读" message="已占用金额和订单数由正式订单事务维护。本页只能维护合同额度，不能覆盖已履约金额。" tone="warning" />
          <div v-if="selectedContract && !isCreatingContract" class="contract-stats">
            <div><span>已下订单数</span><strong>{{ selectedContract.order_count }} 笔</strong></div>
            <div><span>累计占用</span><strong>{{ money(selectedContract.occupied_amount) }}</strong></div>
            <div><span>剩余可用</span><strong>{{ money(selectedContract.remaining_amount) }}</strong></div>
          </div>
          <div v-if="selectedContract && !isCreatingContract" class="master-card__foot">
            <span>第 {{ selectedContract.version }} 版 · 更新于 {{ new Date(selectedContract.updated_at).toLocaleString('zh-CN') }}</span>
          </div>
        </section>

        <section class="master-card master-card--editor">
          <div class="master-card__title"><h2>{{ isCreatingContract ? '新增合同' : '编辑合同' }}</h2><span v-if="selectedContract && !isCreatingContract">版本 {{ selectedContract.version }}</span></div>
          <div v-if="!selectedContract && !isCreatingContract" class="master-empty-state">选择一份合同查看和维护。</div>
          <form v-else class="master-form" @submit.prevent="saveContract">
            <label><span>合同编号</span><input v-model="contractForm.number" required maxlength="64" /></label>
            <label><span>所属部门</span><select v-model="contractForm.department_id" :disabled="!isCreatingContract" required><option v-for="department in departments" :key="department.id" :value="department.id">{{ department.name }}</option></select></label>
            <div class="master-form__row">
              <label><span>业务方向</span><select v-model="contractForm.type"><option value="SALES">销售合同</option><option value="PURCHASE">采购合同</option></select></label>
              <label><span>合同状态</span><select v-model="contractForm.status"><option value="ACTIVE">有效</option><option value="INACTIVE">已停用</option></select></label>
            </div>
            <label><span>签约单位</span><select v-model="contractForm.customer_id" required><option value="" disabled>请选择客户</option><option v-for="customer in lookups.customers" :key="customer.id" :value="customer.id">{{ customer.name }}（{{ customer.code }}）</option></select></label>
            <label><span>工程项目</span><select v-model="contractForm.project_id" required><option value="" disabled>请选择项目</option><option v-for="project in lookups.projects" :key="project.id" :value="project.id">{{ project.name }}（{{ project.code }}）</option></select></label>
            <div class="master-form__row">
              <label><span>合同总金额</span><input v-model="contractForm.total_amount" inputmode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" required /></label>
              <label><span>币种 / 口径</span><input value="CNY / 人民币含税" readonly /></label>
            </div>
            <div class="master-form__row">
              <label><span>合同开始日期</span><input v-model="contractForm.valid_from" type="date" required /></label>
              <label><span>合同结束日期</span><input v-model="contractForm.valid_to" type="date" required /></label>
            </div>
            <div class="master-lines">
              <div class="master-card__title"><h3>合同物料与价格</h3><button type="button" class="text-link" @click="addContractLine">＋ 添加物料</button></div>
              <p class="master-hint">物料和单位沿用标准字典。已存在的合同明细不在此删除，避免影响履约记录。</p>
              <div v-if="!contractForm.lines.length" class="master-empty-inline">尚未配置合同物料明细。</div>
              <div v-for="(line, index) in contractForm.lines" :key="line.line_id ?? `new-${index}`" class="contract-line-editor">
                <label><span>标准物料</span><select v-model="line.material_id" :disabled="Boolean(line.line_id)" required @change="updateLineMaterial(line)"><option value="" disabled>请选择物料</option><option v-for="material in lookups.materials" :key="material.id" :value="material.id">{{ material.code }} · {{ material.name }} {{ material.specification }}</option></select></label>
                <div class="master-form__row"><label><span>单位</span><input :value="line.unit" readonly /></label><label><span>合同单价</span><input v-model="line.unit_price" inputmode="decimal" placeholder="未设置" /></label></div>
                <label><span>数量上限（可选）</span><div class="contract-line-editor__bottom"><input v-model="line.max_quantity" inputmode="decimal" placeholder="不设上限" /><button v-if="line.line_id === null" type="button" class="text-link text-link--danger" @click="removeNewLine(index)">移除</button></div></label>
              </div>
            </div>
            <NoticeBanner v-if="selectedContract && contractForm.total_amount && Number(contractForm.total_amount) < Number(selectedContract.occupied_amount)" title="合同金额低于已占用金额" message="请先核对正式订单和履约金额，再保存合同额度。" tone="danger" />
            <div class="master-form__actions"><span v-if="selectedContract">总额不得低于已占用 {{ money(selectedContract.occupied_amount) }}</span><button type="submit" class="primary-button" :disabled="saving">{{ saving ? '正在保存…' : '保存合同' }}</button></div>
          </form>
        </section>
      </div>
    </template>

    <template v-else>
      <form class="master-filters master-filters--materials" @submit.prevent="loadMaterials">
        <label class="master-filters__wide"><span>编码 / 名称 / 别名</span><input v-model="materialQuery" placeholder="搜索标准名称、业务别名或编码" /></label>
        <label><span>物料状态</span><select v-model="materialActiveFilter"><option value="">全部状态</option><option value="true">启用中</option><option value="false">已停用</option></select></label>
        <button type="submit" class="secondary-button" :disabled="loading">⌕ 查询</button>
      </form>
      <div class="master-workspace master-workspace--materials">
        <section class="master-card master-card--ledger">
          <div class="master-card__title"><h2>标准物料</h2><span>{{ materialRows.length }} 项</span></div>
          <div class="master-table-wrap">
            <table class="master-table"><thead><tr><th>物料编码</th><th>规范名称 / 规格</th><th>单位</th><th>状态</th><th>操作</th></tr></thead>
              <tbody v-if="materialRows.length"><tr v-for="material in materialRows" :key="material.id" :class="{ selected: selectedMaterialId === material.id && !isCreatingMaterial }" @click="openMaterial(material.id)">
                <td><strong>{{ material.code }}</strong><small>别名 {{ material.aliases.length }} 个 · 第 {{ material.version }} 版</small></td>
                <td><strong>{{ material.name }}</strong><small>{{ material.specification }}</small></td>
                <td>{{ material.unit }}</td><td><StatusBadge :label="material.active ? '启用' : '已停用'" :tone="material.active ? 'success' : 'info'" /></td>
                <td><button class="text-link" type="button" @click.stop="openMaterial(material.id)">维护</button></td>
              </tr></tbody>
              <tbody v-else><tr><td colspan="5" class="master-table__empty">{{ loading ? '正在读取物料…' : '没有符合条件的标准物料' }}</td></tr></tbody>
            </table>
          </div>
          <NoticeBanner title="保留多候选" message="相同业务别名可以映射到多个规格。匹配预览会完整列出候选，规格不充分时不会自动绑定编码。" tone="warning" />
          <div class="material-preview">
            <div class="master-card__title"><h3>匹配预览</h3></div>
            <form class="master-preview-form" @submit.prevent="previewMaterialMatch"><label><span>输入业务名称</span><input v-model="previewName" placeholder="例如：螺纹钢" /></label><button class="secondary-button" type="submit" :disabled="previewLoading || !previewName.trim()">{{ previewLoading ? '查询中…' : '预览匹配' }}</button></form>
            <p v-if="previewError" class="master-error">{{ previewError }}</p>
            <p v-else-if="preview && !preview.candidates.length" class="master-hint">未找到匹配物料。</p>
            <div v-else-if="preview?.candidates.length" class="preview-candidates"><div v-for="candidate in preview.candidates" :key="candidate.material_id" class="preview-candidate"><strong>{{ candidate.code }} · {{ candidate.name }}</strong><span>{{ candidate.specification }} · {{ candidate.unit }} · {{ candidate.reason }}</span><StatusBadge v-if="candidate.requires_confirmation" label="需确认规格" tone="warning" /></div></div>
          </div>
        </section>

        <section class="master-card master-card--editor">
          <div class="master-card__title"><h2>{{ isCreatingMaterial ? '新增标准物料' : '维护标准物料' }}</h2><span v-if="selectedMaterial && !isCreatingMaterial">版本 {{ selectedMaterial.version }}</span></div>
          <div v-if="!selectedMaterial && !isCreatingMaterial" class="master-empty-state">选择一个标准物料进行维护。</div>
          <form v-else class="master-form" @submit.prevent="saveMaterial">
            <label><span>标准编码</span><input v-model="materialForm.code" required maxlength="64" /></label>
            <label><span>规范名称</span><input v-model="materialForm.name" required maxlength="256" /></label>
            <div class="master-form__row"><label><span>品牌规格</span><input v-model="materialForm.specification" required maxlength="256" /></label><label><span>主计量单位</span><input v-model="materialForm.unit" required maxlength="16" /></label></div>
            <label><span>业务别名</span><textarea v-model="materialForm.aliases_text" rows="4" placeholder="每行一个别名，也可使用逗号分隔" /></label>
            <p class="master-hint">多个别名分别保存；别名用于搜索候选，不改变标准物料事实。</p>
            <label><span>启用状态</span><select v-model="materialForm.active"><option :value="true">启用</option><option :value="false">已停用</option></select></label>
            <div v-if="selectedMaterial && !isCreatingMaterial" class="master-maintenance-meta"><span>最近维护</span><strong>{{ new Date(selectedMaterial.updated_at).toLocaleString('zh-CN') }}</strong></div>
            <div class="master-form__actions"><span>变更会记录操作人、时间和字段前后版本。</span><button type="submit" class="primary-button" :disabled="saving">{{ saving ? '正在保存…' : '保存映射' }}</button></div>
          </form>
        </section>
      </div>
    </template>
  </section>
</template>

<style scoped>
.master-page { display: grid; gap: 18px; }
.master-page__heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.master-page__heading .eyebrow { margin-bottom: 14px; }
.master-page__heading h1 { margin: 0; color: #203646; font-size: 27px; font-weight: 650; }
.master-page__subtitle { margin: 6px 0 0; color: #71899a; font-size: 12px; }
.master-tabs { display: flex; min-height: 45px; align-items: center; gap: 24px; padding: 0 15px; border: 1px solid #d9e3ea; border-radius: 6px; background: #fff; }
.master-tabs button { align-self: stretch; padding: 0 3px; border: 0; border-bottom: 2px solid transparent; background: none; color: #667f8f; cursor: pointer; font-size: 12px; }
.master-tabs button.active { border-color: #087f8c; color: #087f8c; font-weight: 650; }
.master-tabs__future { margin-left: auto; color: #94a5af; font-size: 11px; }
.master-page__notice { margin-top: -3px; }
.master-filters { display: grid; grid-template-columns: minmax(125px, .75fr) minmax(200px, 1.5fr) minmax(145px, 1fr) minmax(135px, .85fr) auto; align-items: end; gap: 10px; }
.master-filters label, .master-form label, .master-preview-form label { display: grid; min-width: 0; gap: 6px; color: #657d8d; font-size: 11px; }
.master-filters input, .master-filters select, .master-form input, .master-form select, .master-form textarea, .master-preview-form input { width: 100%; min-width: 0; min-height: 39px; padding: 0 11px; border: 1px solid #d9e3ea; border-radius: 5px; outline: none; background: #fff; color: #233d4c; font: inherit; font-size: 12px; }
.master-form textarea { min-height: 100px; padding: 10px 11px; resize: vertical; }
.master-filters input:focus, .master-filters select:focus, .master-form input:focus, .master-form select:focus, .master-form textarea:focus, .master-preview-form input:focus { border-color: #60aeb6; box-shadow: 0 0 0 3px rgb(8 127 140 / 10%); }
.master-filters__wide { min-width: 0; }
.master-filters .secondary-button { min-height: 39px; }
.master-workspace { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(380px, .85fr); align-items: stretch; gap: 16px; }
.master-workspace--materials { grid-template-columns: minmax(0, 1.25fr) minmax(360px, .85fr); }
.master-card { min-width: 0; padding: 19px; border: 1px solid #d9e3ea; border-radius: 6px; background: #fff; }
.master-card__title { display: flex; min-height: 28px; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 13px; }
.master-card__title h2, .master-card__title h3 { margin: 0; color: #263d4d; font-size: 15px; font-weight: 650; }
.master-card__title h3 { font-size: 13px; }
.master-card__title > span { color: #8296a3; font-size: 10px; }
.master-table-wrap { overflow-x: auto; margin-bottom: 15px; }
.master-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.master-table th, .master-table td { min-width: 90px; padding: 11px 10px; border-bottom: 1px solid #e8eef2; text-align: left; vertical-align: middle; }
.master-table th { background: #f5f8fa; color: #728897; font-size: 10px; font-weight: 600; white-space: nowrap; }
.master-table td strong, .master-table td small { display: block; }
.master-table td strong { color: #294351; font-size: 11px; font-weight: 550; }
.master-table td small { margin-top: 5px; color: #8296a3; font-size: 10px; }
.master-table .numeric { text-align: right; white-space: nowrap; }
.master-table tbody tr { cursor: pointer; }
.master-table tbody tr:hover { background: #f5fafb; }
.master-table tbody tr.selected { background: #eaf6f6; }
.master-table__empty { padding: 34px 10px !important; color: #8497a4; text-align: center !important; }
.text-link { padding: 2px 0; border: 0; background: none; color: #087f8c; cursor: pointer; font: inherit; font-size: 11px; }
.text-link:hover { text-decoration: underline; }
.text-link--danger { color: #ad4b45; }
.contract-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 15px; padding: 17px 0 3px; }
.contract-stats div { display: grid; gap: 7px; }
.contract-stats span, .master-card__foot { color: #7e929e; font-size: 10px; }
.contract-stats strong { color: #203847; font-size: 17px; font-weight: 650; }
.master-card__foot { margin-top: 15px; padding-top: 13px; border-top: 1px solid #e7edf1; }
.master-form { display: grid; gap: 12px; }
.master-form__row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.master-form input:disabled, .master-form select:disabled, .master-form input[readonly] { background: #f5f7f8; color: #6f838f; }
.master-form__actions { display: flex; align-items: center; justify-content: space-between; gap: 15px; margin-top: 8px; padding-top: 15px; border-top: 1px solid #e7edf1; }
.master-form__actions > span { color: #8496a2; font-size: 10px; line-height: 1.6; }
.master-form__actions .primary-button { flex: 0 0 auto; }
.master-empty-state, .master-empty-inline { padding: 28px 12px; color: #8396a3; font-size: 12px; text-align: center; }
.master-lines { display: grid; gap: 9px; padding-top: 5px; }
.master-lines .master-card__title { margin-bottom: 0; }
.master-hint { margin: 0; color: #8396a3; font-size: 10px; line-height: 1.65; }
.contract-line-editor { display: grid; gap: 9px; padding: 11px; border: 1px solid #e2eaf0; border-radius: 5px; background: #fbfcfd; }
.contract-line-editor__bottom { display: flex; align-items: center; gap: 12px; }
.contract-line-editor__bottom input { flex: 1; }
.material-preview { padding-top: 13px; border-top: 1px solid #e7edf1; }
.master-preview-form { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: 9px; }
.master-preview-form .secondary-button { min-height: 39px; }
.preview-candidates { display: grid; gap: 8px; margin-top: 12px; }
.preview-candidate { display: grid; gap: 5px; padding: 9px 10px; border: 1px solid #e2eaf0; border-radius: 5px; background: #fbfcfd; }
.preview-candidate strong { color: #2a4655; font-size: 11px; }
.preview-candidate span { color: #718796; font-size: 10px; }
.master-maintenance-meta { display: grid; gap: 6px; padding: 12px; border-radius: 5px; background: #f5f8fa; color: #7a8f9c; font-size: 10px; }
.master-maintenance-meta strong { color: #314b59; font-size: 11px; font-weight: 550; }
.master-error { color: #a63e38; font-size: 11px; }

@media (max-width: 1120px) { .master-workspace, .master-workspace--materials { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 760px) {
  .master-page__heading { align-items: flex-start; flex-direction: column; }
  .master-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .master-filters__wide { grid-column: 1 / -1; }
  .master-filters > button { justify-self: start; }
  .master-tabs { overflow-x: auto; gap: 16px; white-space: nowrap; }
  .master-tabs__future { display: none; }
  .master-form__row { grid-template-columns: 1fr; }
  .contract-stats { gap: 8px; }
  .contract-stats strong { font-size: 14px; }
}
</style>
