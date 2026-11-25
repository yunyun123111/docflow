import vue from '@vitejs/plugin-vue'
import { viteStaticCopy } from 'vite-plugin-static-copy'
import { defineConfig } from 'vite'

// cmaps / standard_fonts 是 pdf.js 渲染中文与非内嵌字体 PDF 的必需资源，
// 一起打进 dist，桌面版 exe 离线也能正常预览（不走任何 CDN）。
export default defineConfig({
  plugins: [
    vue(),
    viteStaticCopy({
      targets: [
        // stripBase: true 才是扁平复制，否则会把 node_modules/... 整段路径带进 dest，
        // 导致 /pdfjs/cmaps/*.bcmap 404，中文（非内嵌 CID 字体）PDF 渲染出空白字形。
        { src: 'node_modules/pdfjs-dist/cmaps/*', dest: 'pdfjs/cmaps', rename: { stripBase: true } },
        { src: 'node_modules/pdfjs-dist/standard_fonts/*', dest: 'pdfjs/standard_fonts', rename: { stripBase: true } },
      ],
    }),
  ],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  build: {
    chunkSizeWarningLimit: 1400,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router', 'pinia', 'axios'],
          ui: ['element-plus', '@element-plus/icons-vue'],
          charts: ['echarts'],
          pdf: ['pdfjs-dist'],
        },
      },
    },
  },
})