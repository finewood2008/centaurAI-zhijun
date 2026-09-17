'use strict'
// Build the independent Zhijun mark from shared, font-independent vector paths.
// Run on macOS; the generated assets are checked in for other build platforms.
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

if (process.platform !== 'darwin') throw new Error('Icon conversion requires macOS Swift, sips and iconutil; use the checked-in assets on other platforms.')
const source = path.resolve(__dirname, '../../assets/zhijun-mark.json')
const renderer = path.join(__dirname, 'render-zhijun-icon.swift')
const output = path.resolve(__dirname, '../assets')
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-mark-icons-'))
const run = (command, args) => execFileSync(command, args, { stdio: 'pipe' })
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')

try {
  const iconset = path.join(temporary, 'zhijun.iconset')
  fs.mkdirSync(iconset)
  const png = path.join(temporary, 'zhijun.png')
  run('/usr/bin/swift', ['-module-cache-path', path.join(temporary, 'swift-cache'), renderer, source, png])
  const mark = JSON.parse(fs.readFileSync(source, 'utf8'))
  const paths = data => data.map(commands => `<path d="${commands.map(command => command.join(' ')).join(' ')}"/>`).join('')
  const glyph = `<g fill="${mark.color}">${paths(mark.paths)}</g>`
  const svg = contents => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="512" height="512"><title>知君</title>${contents}</svg>\n`
  const webAssets = path.resolve(__dirname, '../../mindos-web/src/assets')
  fs.mkdirSync(webAssets, { recursive: true })
  fs.writeFileSync(path.join(webAssets, 'zhijun-mark.svg'), svg(glyph))
  fs.writeFileSync(path.join(webAssets, 'zhijun-wordmark.svg'), `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${mark.wordmark.width} ${mark.wordmark.height}"><title>知君</title><g fill="#292D29">${paths(mark.wordmark.paths)}</g></svg>\n`)
  fs.writeFileSync(path.resolve(__dirname, '../../mindos-web/public/icon.svg'), svg(`<rect width="100" height="100" rx="22" fill="${mark.background}"/>${glyph}`))
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
  const icns = path.join(temporary, 'zhijun.icns')
  run('/usr/bin/iconutil', ['-c', 'icns', iconset, '-o', icns])
  fs.mkdirSync(output, { recursive: true })
  fs.copyFileSync(png, path.join(output, 'zhijun.png'))
  fs.copyFileSync(icns, path.join(output, 'zhijun.icns'))
  fs.writeFileSync(path.join(output, 'zhijun-source.json'), JSON.stringify({
    source: '../../assets/zhijun-mark.json', sourceSha256: digest(source),
    renderer: '../scripts/render-zhijun-icon.swift', rendererSha256: digest(renderer),
    buildScript: '../scripts/build-icons.cjs', buildScriptSha256: digest(__filename),
    conversion: 'Shared vector paths on a warm ivory rounded tile; CoreGraphics RGBA, sips and iconutil standard sizes',
    layout: { canvas: 1024, tileSize: 880, tileRadius: 190, mark },
    files: { 'zhijun.png': digest(png), 'zhijun.icns': digest(icns) },
  }, null, 2) + '\n')
  console.log('Built Zhijun vector mark, browser SVG, PNG and ICNS application icons.')
} finally {
  fs.rmSync(temporary, { recursive: true, force: true })
}
