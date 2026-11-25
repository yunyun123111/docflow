<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { mediaUrl } from '../api/client'

/** 文档缩略图：懒加载（进入视口才请求），只有 PDF 与图片可服务端出图，其余直接显示类型徽标。 */
const props = withDefaults(defineProps<{
  docId: number
  fileType?: string
  size?: number
  round?: boolean
  exists?: boolean
}>(), { fileType: '', size: 150, round: false, exists: true })

const EMOJI: Record<string, string> = {
  pdf: '📄', image: '🖼️', docx: '📝', doc: '📝', xlsx: '📊', xlsm: '📊',
  xls: '📊', csv: '🧮', txt: '📃', md: '📃',
}

// 后端只对这两类出图，其余类型请求必然 404，直接跳过，省请求也省控制台报错。
// 文件已被外部删除（文档库「文件缺失」）时同样跳过：缩略图缓存也救不了它。
const THUMBABLE = new Set(['pdf', 'image'])
const canThumb = computed(() => THUMBABLE.has(props.fileType) && props.exists)

const box = ref<HTMLElement>()
const url = ref('')
const visible = ref(false)
const failed = ref(false)
let io: IntersectionObserver | null = null

async function fetchThumb() {
  if (!canThumb.value || failed.value || url.value) return
  try {
    url.value = await mediaUrl(`/api/preview/${props.docId}/thumbnail?size=${Math.max(64, Math.min(640, props.size * 2))}`)
  } catch {
    failed.value = true
  }
}

watch(visible, (v) => { if (v) fetchThumb() })

onBeforeUnmount(() => io?.disconnect())
onMounted(observe)

function observe() {
  io?.disconnect()
  if (!box.value) return
  io = new IntersectionObserver((entries) => {
    visible.value = entries.some((e) => e.isIntersecting)
  }, { rootMargin: '240px' })
  io.observe(box.value)
}

watch(() => props.docId, async () => {
  url.value = ''
  failed.value = false
  visible.value = false
  await nextTick()
  observe()
})
</script>

<template>
  <div ref="box" class="thumb" :class="{ round }" :style="{ height: size + 'px' }">
    <img v-if="url" :src="url" alt="" loading="lazy" />
    <div v-else-if="!canThumb || failed" class="badge"><span>{{ EMOJI[fileType] || '📎' }}</span></div>
    <div v-else-if="!visible" class="sk" />
    <div v-else class="badge"><span>{{ EMOJI[fileType] || '📎' }}</span></div>
  </div>
</template>

<style scoped>
.thumb { position: relative; overflow: hidden; background: var(--df-well); border-radius: 12px; }
.thumb.round { border-radius: 6px; }
.thumb img { width: 100%; height: 100%; object-fit: contain; object-position: top center; display: block;
  padding: 5px; box-sizing: border-box; filter: drop-shadow(0 2px 6px rgba(16, 24, 32, .14)); }
.badge { height: 100%; display: flex; align-items: center; justify-content: center; font-size: 30px; }
.sk { height: 100%; background: linear-gradient(100deg, var(--el-fill-color-light) 30%, var(--el-fill-color) 50%, var(--el-fill-color-light) 70%);
  background-size: 200% 100%; animation: sk 1.1s infinite ease; }
@keyframes sk { from { background-position: 120% 0 } to { background-position: -80% 0 } }
</style>