import { defineStore } from 'pinia'

export type Identity = {
  user_id: string
  login: string
  roles: string[]
  department_ids: string[]
  csrf_token: string
}

type ApiError = { error?: { code?: string; message?: string } }
type IdentityResponse = { data: Identity }

function responseError(body: ApiError, fallback: string) {
  return {
    code: body.error?.code ?? 'REQUEST_FAILED',
    message: body.error?.message ?? fallback,
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    identity: null as Identity | null,
    initialized: false,
    loading: false,
    error: '',
    errorCode: '',
  }),
  actions: {
    async loadIdentity(force = false) {
      if (this.initialized && !force) return
      this.loading = true
      this.error = ''
      this.errorCode = ''
      try {
        const response = await fetch('/api/v1/auth/me', {
          headers: { Accept: 'application/json' },
        })
        const body = (await response.json()) as IdentityResponse & ApiError
        if (!response.ok) {
          const failure = responseError(body, '无法读取登录状态，请稍后重试。')
          if (response.status === 401) {
            this.identity = null
            this.errorCode = failure.code
          } else {
            this.error = failure.message
            this.errorCode = failure.code
          }
          return
        }
        this.identity = body.data
      } catch {
        this.identity = null
        this.errorCode = 'NETWORK_ERROR'
        this.error = '暂时无法连接登录服务，请检查网络后重试。'
      } finally {
        this.initialized = true
        this.loading = false
      }
    },

    async login(login: string, password: string) {
      this.loading = true
      this.error = ''
      this.errorCode = ''
      try {
        const response = await fetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
          body: JSON.stringify({ login, password }),
        })
        const body = (await response.json()) as IdentityResponse & ApiError
        if (!response.ok) {
          const failure = responseError(body, '登录失败，请稍后重试。')
          this.error = failure.code === 'INVALID_CREDENTIALS' ? '账号或密码错误。' : failure.message
          this.errorCode = failure.code
          return false
        }
        this.identity = body.data
        this.initialized = true
        return true
      } catch {
        this.error = '暂时无法连接登录服务，请检查网络后重试。'
        this.errorCode = 'NETWORK_ERROR'
        return false
      } finally {
        this.loading = false
      }
    },

    async logout() {
      if (!this.identity) {
        return true
      }
      if (!this.identity.csrf_token) {
        this.error = '当前登录状态缺少安全校验信息，请刷新页面后重试。'
        return false
      }
      this.loading = true
      this.error = ''
      try {
        const response = await fetch('/api/v1/auth/logout', {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'X-CSRF-Token': this.identity.csrf_token,
          },
        })
        if (!response.ok) {
          const body = (await response.json()) as ApiError
          this.error = responseError(body, '退出登录失败，请重试。').message
          return false
        }
        this.identity = null
        this.initialized = true
        return true
      } catch {
        this.error = '暂时无法连接登录服务，当前会话尚未确认退出。'
        return false
      } finally {
        this.loading = false
      }
    },
  },
})

export const roleLabels: Record<string, string> = {
  business_operator: '业务经办人',
  business_approver: '业务审批人',
  system_admin: '系统管理员',
}
