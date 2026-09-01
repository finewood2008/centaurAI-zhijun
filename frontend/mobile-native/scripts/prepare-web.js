const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const frontendRoot = path.resolve(root, '..');
const outDir = path.join(root, 'www');

function copyDir(src, dest) {
  if (!fs.existsSync(src)) throw new Error(`Missing source directory: ${src}`);
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const from = path.join(src, entry.name);
    const to = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(from, to);
    else if (entry.isFile()) fs.copyFileSync(from, to);
  }
}

fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

copyDir(path.join(frontendRoot, 'mobile'), path.join(outDir, 'mobile'));
copyDir(path.join(frontendRoot, 'assets'), path.join(outDir, 'assets'));

fs.writeFileSync(
  path.join(outDir, 'index.html'),
  '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=/mobile/">',
  'utf8'
);

console.log(`Prepared native web assets in ${outDir}`);
