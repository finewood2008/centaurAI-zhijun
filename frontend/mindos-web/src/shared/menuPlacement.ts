type Bounds = { left: number; right: number; top: number; bottom: number }

// Keep the menu inside the reading area, flipping above the trigger near the composer.
export function menuPlacement(anchor: Bounds, size: { width: number; height: number }, bounds: Bounds) {
  const gap = 4
  const below = Math.max(0, bounds.bottom - anchor.bottom - gap)
  const above = Math.max(0, anchor.top - bounds.top - gap)
  const opensAbove = size.height > below && above > below
  const maxHeight = opensAbove ? above : below
  const height = Math.min(size.height, maxHeight)
  const width = Math.min(size.width, bounds.right - bounds.left)
  return {
    left: Math.max(bounds.left, Math.min(anchor.right - width, bounds.right - width)),
    top: Math.max(bounds.top, Math.min(opensAbove ? anchor.top - gap - height : anchor.bottom + gap, bounds.bottom - height)),
    maxHeight,
  }
}
