<script setup lang="ts">
import { useRuntimeStore } from '../stores/runtime'

const runtime = useRuntimeStore()
</script>

<template>
  <main class="min-h-screen bg-slate-50 px-6 py-12 text-slate-900">
    <section class="mx-auto max-w-4xl rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
      <p class="text-sm font-semibold tracking-wide text-teal-700">TRADEPILOT · 工程基线</p>
      <h1 class="mt-3 text-3xl font-semibold">智能商贸提单工作台</h1>
      <p class="mt-3 max-w-2xl text-slate-600">
        目前提供工程启动与服务状态检查。订单、模型工具及文件解析能力尚未接入。
      </p>

      <div class="mt-8 grid gap-4 sm:grid-cols-3">
        <div class="rounded-xl bg-slate-50 p-4">
          <h2 class="text-sm font-medium text-slate-500">API 与数据库</h2>
          <p class="mt-2 font-semibold" aria-live="polite">
            {{ runtime.loading ? '检查中' : runtime.health?.status === 'ready' ? '已就绪' : '未连接' }}
          </p>
        </div>
        <div class="rounded-xl bg-slate-50 p-4">
          <h2 class="text-sm font-medium text-slate-500">模型服务</h2>
          <p class="mt-2 font-semibold">
            {{ runtime.configuration?.model.status === 'configured_unverified' ? '已填写设置，尚未验证' : '未配置' }}
          </p>
        </div>
        <div class="rounded-xl bg-slate-50 p-4">
          <h2 class="text-sm font-medium text-slate-500">OCR 服务</h2>
          <p class="mt-2 font-semibold">
            {{ runtime.configuration?.ocr.status === 'configured_unverified' ? '已填写设置，尚未验证' : '未配置' }}
          </p>
        </div>
      </div>

      <p v-if="runtime.error" role="status" class="mt-6 text-sm text-rose-700">
        {{ runtime.error }}
      </p>
      <button
        class="mt-6 rounded-lg bg-teal-700 px-4 py-2 text-sm font-medium text-white hover:bg-teal-800 focus:outline-2 focus:outline-offset-2 focus:outline-teal-700"
        type="button"
        :disabled="runtime.loading"
        @click="runtime.refresh()"
      >
        重新检查服务
      </button>
    </section>
  </main>
</template>
