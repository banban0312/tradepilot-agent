import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue(), tailwindcss()],
    server: {
      host: '0.0.0.0',
      proxy: {
        '/api': {
          target:
            process.env.TRADEPILOT_API_PROXY_TARGET ||
            env.TRADEPILOT_API_PROXY_TARGET ||
            'http://127.0.0.1:8010',
          changeOrigin: true,
          rewrite: (path) =>
            path.startsWith('/api/v1/') ? path : path.replace(/^\/api/, ''),
        },
      },
    },
  }
})
