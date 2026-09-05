import assert from 'node:assert/strict'
import { cardBodyPreview, stripCardFrontmatter, stripCardHeading } from '../src/shared/cardContent.ts'

assert.equal(stripCardFrontmatter('---\ntitle: "A"\n---\n# A\n\nBody'), '# A\n\nBody')
assert.equal(stripCardHeading('# A\n\nBody'), '\nBody')
assert.equal(cardBodyPreview('---\ntitle: "A"\n---\n# A\n\nBody text'), 'Body text')
assert.equal(cardBodyPreview('# A\n\nfirst\n---\nsecond'), 'first --- second')
assert.equal(cardBodyPreview('# A\n\n1234567890', 5), '12345…')
console.log('card-content: 5 tests OK')
