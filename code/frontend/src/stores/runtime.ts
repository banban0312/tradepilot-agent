import { defineStore } from 'pinia'

type ServiceHealth = { status: string; database?: string }
type IntegrationState = { status: string; provider?: string; model?: string }
type RuntimeConfiguration = { model: IntegrationState; ocr: IntegrationState }

export const useRuntimeStore = defineStore('runtime', {
  state: () => ({
    health: null as ServiceHealth | null,
    configuration: null as RuntimeConfiguration | null,
    loading: false,
    error: '',
  }),
  actions: {
    async refresh() {
      this.loading = true
      this.error = ''
      try {
        const [healthResponse, configurationResponse] = await Promise.all([
          fetch('/api/health/ready'),
          fetch('/api/health/config'),
        ])
        if (!healthResponse.ok || !configurationResponse.ok) {
          throw new Error('后端服务尚未就绪')
        }
        this.health = (await healthResponse.json()) as ServiceHealth
        this.configuration = (await configurationResponse.json()) as RuntimeConfiguration
      } catch (error) {
        this.error = error instanceof Error ? error.message : '目前无法连接至后端'
        this.health = null
        this.configuration = null
      } finally {
        this.loading = false
      }
    },
  },
})
