import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// pywebview 通过 file:// 加载 dist/index.html，必须使用相对 base。
export default defineConfig({
  plugins: [
    vue({
      template: {
        compilerOptions: {
          // 开屏动画是原生 Web Component（Shadow DOM 自隔离），不要按 Vue 组件解析。
          isCustomElement: (tag) => tag === 'keyrealm-splash',
        },
      },
    }),
    // Element Plus 按需引入：模板里出现的 el-* 组件（含 v-loading 指令）在编译期
    // 自动 import 组件本体与其样式，取代原先 main.js 里整包 app.use(ElementPlus)
    // + element-plus/dist/index.css。
    //
    // dirs: [] —— 本项目的自有组件（AppLayout/ThemePicker/…）都在各自 SFC 里显式
    // import，依赖关系可见；关掉目录扫描，让这个插件只负责 Element Plus。
    // dts: false —— 纯 JS 工程，不生成 components.d.ts。
    //
    // 注意：命令式服务（ElMessage / ElMessageBox）不是模板标签，解析器扫不到，
    // 它们的样式在 main.js 里手动引入对应入口。
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
      dirs: [],
      dts: false,
    }),
  ],
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
    chunkSizeWarningLimit: 2000,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
})