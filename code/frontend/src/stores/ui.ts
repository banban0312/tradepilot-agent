import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', {
  state: () => ({ toast: '' as string }),
  actions: {
    notify(message: string) {
      this.toast = message
      window.setTimeout(() => {
        if (this.toast === message) this.toast = ''
      }, 4200)
    },
    clearToast() {
      this.toast = ''
    },
  },
})
