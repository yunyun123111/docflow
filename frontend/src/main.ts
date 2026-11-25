import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './styles/main.css'

// pdf.js 4.x 依赖 Promise.withResolvers；旧版 WebView2/浏览器没有，这里补齐
if (typeof (Promise as any).withResolvers !== 'function') {
  (Promise as any).withResolvers = function withResolvers<T>() {
    let resolve!: (v: T | PromiseLike<T>) => void
    let reject!: (reason?: unknown) => void
    const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }
}

const stored = localStorage.getItem('docflow_theme')
if (stored === 'dark' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
  document.documentElement.classList.add('dark')
}

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')