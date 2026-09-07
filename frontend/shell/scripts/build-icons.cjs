'use strict'
// Deterministic framing of the original artwork; no generated replacement art.
// Run on macOS; the generated assets are checked in for other build platforms.
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

if (process.platform !== 'darwin') throw new Error('Icon conversion requires macOS Swift, sips and iconutil; use the checked-in assets on other platforms.')
const source = path.resolve(__dirname, '../../mindos-web/logo.jpg')
const renderer = path.join(__dirname, 'render-centaur-icon.swift')
const output = path.resolve(__dirname, '../assets')
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-centaur-icons-'))
const run = (command, args) => execFileSync(command, args, { stdio: 'pipe' })
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')

try {
  const iconset = path.join(temporary, 'centaur.iconset')
  fs.mkdirSync(iconset)
  const png = path.join(temporary, 'centaur.png')
  const layout = path.join(temporary, 'layout.json')
  run('/usr/bin/swift', ['-module-cache-path', path.join(temporary, 'swift-cache'), renderer, source, png, layout])
  const rendered = fs.readFileSync(png)
  if (rendered.readUInt32BE(16) !== 1024 || rendered.readUInt32BE(20) !== 1024 || rendered[25] !== 6) {
    throw new Error('The rendered icon must be a 1024 square RGBA PNG.')
  }
  for (const size of [16, 32, 128, 256, 512]) {
    for (const scale of [1, 2]) {
      const name = `icon_${size}x${size}${scale === 2 ? '@2x' : ''}.png`
      const destination = path.join(iconset, name)
      if (size * scale === 1024) fs.copyFileSync(png, destination)
      else run('/usr/bin/sips', ['-z', String(size * scale), String(size * scale), png, '--out', destination])
    }
  }
  const icns = path.join(temporary, 'centaur.icns')
  run('/usr/bin/iconutil', ['-c', 'icns', iconset, '-o', icns])
  fs.mkdirSync(output, { recursive: true })
  fs.copyFileSync(png, path.join(output, 'centaur.png'))
  fs.copyFileSync(icns, path.join(output, 'centaur.icns'))
  fs.writeFileSync(path.join(output, 'centaur-source.json'), JSON.stringify({
    source: '../../mindos-web/logo.jpg', sourceSha256: digest(source),
    renderer: '../scripts/render-centaur-icon.swift', rendererSha256: digest(renderer),
    buildScript: '../scripts/build-icons.cjs', buildScriptSha256: digest(__filename),
    conversion: 'CoreGraphics near-white background mask; original centaur proportionally centered on a warm ivory rounded tile, transparent surround and soft shadow; sips and iconutil standard sizes',
    layout: JSON.parse(fs.readFileSync(layout, 'utf8')),
    files: { 'centaur.png': digest(png), 'centaur.icns': digest(icns) },
  }, null, 2) + '\n')
  console.log('Converted the existing centaur image into PNG and ICNS application icons.')
} finally {
  fs.rmSync(temporary, { recursive: true, force: true })
}
