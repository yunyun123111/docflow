import { onBeforeUnmount, onMounted, ref } from 'vue'

export type Breakpoint = 'xs' | 'sm' | 'md' | 'lg' | 'xl'

const POINTS: Record<Breakpoint, number> = { xs: 0, sm: 640, md: 860, lg: 1080, xl: 1280 }

function which(w: number): Breakpoint {
  if (w >= POINTS.xl) return 'xl'
  if (w >= POINTS.lg) return 'lg'
  if (w >= POINTS.md) return 'md'
  if (w >= POINTS.sm) return 'sm'
  return 'xs'
}

/** 全局唯一的断点状态：布局、弹窗宽度、表格列都按它自适应。 */
const width = ref(window.innerWidth)
const bp = ref<Breakpoint>(which(window.innerWidth))
let listeners = 0

function measure() {
  width.value = window.innerWidth
  bp.value = which(window.innerWidth)
}

export function useResponsive() {
  onMounted(() => {
    if (!listeners) window.addEventListener('resize', measure)
    listeners += 1
  })
  onBeforeUnmount(() => {
    listeners -= 1
    if (!listeners) window.removeEventListener('resize', measure)
  })
  return {
    width,
    bp,
    isMobile: () => bp.value === 'xs' || bp.value === 'sm',
    isNarrow: () => POINTS[bp.value] < POINTS.lg,
    isCompact: () => POINTS[bp.value] < POINTS.md,
    /** 弹窗宽度：窄屏几乎全屏，宽屏留白更好看。 */
    dialogWidth: (base = '900px') => {
      const w = width.value
      if (w < 720) return '96%'
      if (w < 1100) return '92%'
      if (w < 1500) return '86%'
      return base
    },
    dialogHeight: (base = '78vh') => (POINTS[bp.value] < POINTS.md ? '86vh' : base),
  }
}

export { POINTS as BREAKPOINTS }