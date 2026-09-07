'use strict'
// Format conversion only: preserve the original centaur's full composition.
// Run on macOS; the generated assets are checked in for other build platforms.
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

if (process.platform !== 'darwin') throw new Error('Icon conversion requires macOS sips and iconutil; use the checked-in assets on other platforms.')
const source = path.resolve(__dirname, '../../mindos-web/logo.jpg')
const output = path.resolve(__dirname, '../assets')
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-centaur-icons-'))
const run = (command, args) => execFileSync(command, args, { stdio: 'pipe' })
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')

try {
  const iconset = path.join(temporary, 'centaur.iconset')
  fs.mkdirSync(iconset)
  const png = path.join(temporary, 'centaur.png')
  run('/usr/bin/sips', ['-s', 'format', 'png', source, '--out', png])
  // The source is portrait. Fit it within 1024² and pad the existing white
  // background; never stretch, crop, remove its background or redraw it.
  run('/usr/bin/sips', ['-Z', '1024', png])
  run('/usr/bin/sips', ['--padToHeightWidth', '1024', '1024', '--padColor', 'FFFFFF', png])
  for (const size of [16, 32, 128, 256, 512]) {
    for (const scale of [1, 2]) {
      const name = `icon_${size}x${size}${scale === 2 ? '@2x' : ''}.png`
      const destination = path.join(iconset, name)
      run('/usr/bin/sips', ['-z', String(size * scale), String(size * scale), png, '--out', destination])
    }
  }
  const icns = path.join(temporary, 'centaur.icns')
  run('/usr/bin/iconutil', ['-c', 'icns', iconset, '-o', icns])
  fs.mkdirSync(output, { recursive: true })
  fs.copyFileSync(png, path.join(output, 'centaur.png'))
  fs.copyFileSync(icns, path.join(output, 'centaur.icns'))
  fs.writeFileSync(path.join(output, 'centaur-source.json'), JSON.stringify({
    source: '../../mindos-web/logo.jpg', sourceSha256: digest(source),
    conversion: 'macOS sips: preserve aspect ratio, fit within 1024 square, white padding; iconutil standard sizes',
    files: { 'centaur.png': digest(png), 'centaur.icns': digest(icns) },
  }, null, 2) + '\n')
  console.log('Converted the existing centaur image into PNG and ICNS application icons.')
} finally {
  fs.rmSync(temporary, { recursive: true, force: true })
}
