import axios from 'axios'
import { ElMessage } from 'element-plus'

export const TOKEN_KEY = 'docflow_token'

const http = axios.create({ baseURL: '/api', timeout: 60000 })

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (r) => r.data,
  (error) => {
    const status = error?.response?.status
    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      window.dispatchEvent(new CustomEvent('docflow-unauthorized'))
      return Promise.reject(error)
    }
    // 404 常见于「该文档没有缩略图」这类可预期分支，不打扰用户
    if (!(status === 404 || (status === 400 && error?.config?.url?.includes('/preview/')))) {
      const msg = error?.response?.data?.detail || error?.message || '请求失败'
      ElMessage.error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    return Promise.reject(error)
  },
)

export function authHeaders(): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY)
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** object URL 缓存：缩略图这类小图反复滚动时不再重复请求。 */
const blobCache = new Map<string, string>()
const blobPending = new Map<string, Promise<string>>()

export async function mediaUrl(url: string): Promise<string> {
  const hit = blobCache.get(url)
  if (hit) return hit
  const pending = blobPending.get(url)
  if (pending) return pending
  const task = (async () => {
    const resp = await fetch(url, { headers: authHeaders() as any })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const href = URL.createObjectURL(await resp.blob())
    blobCache.set(url, href)
    if (blobCache.size > 400) {
      const [firstKey, firstHref] = blobCache.entries().next().value
      URL.revokeObjectURL(firstHref)
      blobCache.delete(firstKey)
    }
    return href
  })().finally(() => blobPending.delete(url))
  blobPending.set(url, task)
  return task
}

/** 桌面版（DocFlow.exe + pywebview）才有的本机能力；浏览器形态自动降级。 */
export const desktop = {
  get api(): any {
    return (window as any).pywebview?.api
  },
  get available(): boolean {
    return !!(window as any).pywebview?.api
  },
  async pickDirectory(title = '选择目录', startPath = ''): Promise<string | null> {
    const r = await this.api?.pick_directory?.(title, startPath)
    if (!r?.ok && r?.error) ElMessage.warning(r.error)
    return r?.ok ? (r.path || '') : null
  },
  async revealDocument(docId: number): Promise<boolean> {
    const r = await this.api?.reveal_document?.(docId)
    if (!r?.ok && r?.error) ElMessage.warning(r.error)
    return !!r?.ok
  },
  async quit(): Promise<void> {
    await this.api?.quit?.()
  },
}

export default http as any