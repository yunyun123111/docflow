/** 时间显示统一收口：后端给的是 ISO（带 T），界面里只保留到分钟。 */
export function fmtTime(iso?: string | null, withSeconds = false): string {
  if (!iso) return '—'
  const s = String(iso).replace('T', ' ')
  return withSeconds ? s.slice(0, 19) : s.slice(0, 16)
}