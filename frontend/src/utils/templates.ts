/** 模板预设、占位符元数据与实时示例；渲染口径对齐 backend/services/naming.py。 */

export type TemplateMode = 'path' | 'name'

export interface TemplatePreset { label: string; value: string }
export interface TemplateToken { token: string; label: string }

/** 固定样例：不必先跑一遍真实文档，输入时就能看到归档后长什么样。 */
export const SAMPLE: Record<string, string> = {
  category: '采购合同',
  type: '采购合同',
  company: '华信实业有限公司',
  counterparty: '大连盛通物资有限公司',
  contract_no: 'HXCG-2026-088',
  amount: '128600',
  date: '20261124',
  year: '2026',
  month: '11',
  day: '26',
  title: '设备采购合同',
  orig: '扫描件_20261124',
}

export const ARCHIVE_PRESETS: TemplatePreset[] = [
  { label: '类别 / 年 / 月', value: '{category}/{year}/{month}' },
  { label: '类别 / 年', value: '{category}/{year}' },
  { label: '年 / 类别', value: '{year}/{category}' },
  { label: '仅按类别', value: '{category}' },
]

export const ARCHIVE_TOKENS: TemplateToken[] = [
  { token: '{category}', label: '类别' },
  { token: '{year}', label: '年' },
  { token: '{month}', label: '月' },
  { token: '{day}', label: '日' },
  { token: '{company}', label: '公司' },
  { token: '{contract_no}', label: '合同号' },
  { token: '{type}', label: '单据类型' },
  { token: '{date}', label: '文档日期' },
]

export const RENAME_PRESETS: TemplatePreset[] = [
  { label: '日期_合同号_标题', value: '{date}_{contract_no}_{title}' },
  { label: '日期_标题', value: '{date}_{title}' },
  { label: '合同号_标题', value: '{contract_no}_{title}' },
  { label: '保持原文件名', value: '{orig}' },
]

export const RENAME_TOKENS: TemplateToken[] = [
  { token: '{date}', label: '文档日期' },
  { token: '{company}', label: '公司' },
  { token: '{counterparty}', label: '对方单位' },
  { token: '{contract_no}', label: '合同号' },
  { token: '{amount}', label: '金额' },
  { token: '{type}', label: '单据类型' },
  { token: '{title}', label: '标题' },
  { token: '{orig}', label: '原文件名' },
]

// {underscore} 是旧写法：仍参与渲染，但不再作为可选项露出。
const KNOWN = new Set([...Object.keys(SAMPLE), 'underscore'])

function segmentValue(key: string): string {
  if (key === 'underscore') return '_'
  return KNOWN.has(key) ? SAMPLE[key] : ''
}

/** 逐段替换：占位符之外的字面量原样保留，未闭合的 { 之后按空处理。 */
function renderRaw(pattern: string): string {
  const out: string[] = []
  let rest = pattern
  while (rest.includes('{')) {
    const open = rest.indexOf('{')
    out.push(rest.slice(0, open))
    const tail = rest.slice(open + 1)
    const close = tail.indexOf('}')
    if (close === -1) {
      out.push(segmentValue(tail.trim()))
      rest = ''
      break
    }
    out.push(segmentValue(tail.slice(0, close).trim()))
    rest = tail.slice(close + 1)
  }
  out.push(rest)
  return out.join('')
}

/** 连续下划线折叠 + 首尾 _ . - 和空格裁掉，与后端一致。 */
function collapse(text: string): string {
  return text.replace(/_{2,}/g, '_').replace(/^[\s_.-]+/, '').replace(/[\s_.-]+$/, '')
}

export function previewName(template: string, ext = '.pdf'): string {
  const base = collapse(renderRaw(template || ''))
  return (base || SAMPLE.orig) + ext
}

export function previewSegments(template: string): string[] {
  const segments: string[] = []
  for (const raw of (template || '').split('/')) {
    if (!raw.trim()) continue
    const seg = collapse(renderRaw(raw.trim()))
    if (seg) segments.push(seg)
  }
  return segments
}

export function unknownTokens(template: string): string[] {
  const found: string[] = []
  for (const m of (template || '').matchAll(/\{([^{}]*)\}/g)) {
    const key = (m[1] || '').trim()
    if (!KNOWN.has(key) && !found.includes(m[0])) found.push(m[0])
  }
  return found
}
