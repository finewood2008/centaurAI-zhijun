export function stripCardFrontmatter(content: string): string {
  const normalized = String(content ?? '').replace(/^\uFEFF/, '')
  const match = normalized.match(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/)
  return match ? normalized.slice(match[0].length) : normalized
}

export function stripCardHeading(content: string): string {
  return content.replace(/^\s*#\s+[^\r\n]+(?:\r?\n|$)/, '')
}

export function cardBodyPreview(content: string, maxLength = 120): string {
  const body = stripCardHeading(stripCardFrontmatter(content)).replace(/\s+/g, ' ').trim()
  if (!body) return ''
  return body.length > maxLength ? `${body.slice(0, maxLength).trimEnd()}…` : body
}
