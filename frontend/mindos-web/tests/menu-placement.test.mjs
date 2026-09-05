import assert from 'node:assert/strict'
import { menuPlacement } from '../src/shared/menuPlacement.ts'

const bounds = { left: 800, right: 1680, top: 160, bottom: 750 }
const size = { width: 224, height: 180 }
// The screenshot's left-edge trigger must never push the first characters outside the stream.
let result = menuPlacement({ left: 920, right: 948, top: 450, bottom: 476 }, size, bounds)
assert.equal(result.left, 800)
assert.equal(result.top, 480)

// At the composer boundary, open above, wholly inside the reading area.
result = menuPlacement({ left: 920, right: 948, top: 714, bottom: 740 }, size, bounds)
assert.equal(result.top, 530)
assert.ok(result.top + size.height < 714)

// Right-edge placement, very narrow mobile panes, and long menus remain bounded.
for (const area of [bounds, { left: 20, right: 370, top: 200, bottom: 650 }, { left: 20, right: 200, top: 200, bottom: 450 }]) {
  for (const height of [180, 900]) {
    for (const top of [area.top + 4, (area.top + area.bottom) / 2, area.bottom - 30]) {
      for (const left of [area.left, area.right - 28]) {
        const placement = menuPlacement({ left, right: left + 28, top, bottom: top + 26 }, { width: 224, height }, area)
        assert.ok(placement.left >= area.left)
        assert.ok(placement.left + Math.min(224, area.right - area.left) <= area.right)
        assert.ok(placement.top >= area.top)
        assert.ok(placement.top + Math.min(height, placement.maxHeight) <= area.bottom)
        assert.ok(placement.maxHeight >= 0)
      }
    }
  }
}
console.log('menu placement: clipped left edge, composer flip, right edge, mobile and long menus passed')
