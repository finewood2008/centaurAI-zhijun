'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFile, execFileSync } = require('node:child_process');
const { promisify } = require('node:util');

// Node's Windows stat.mode describes DOS attributes, not the NTFS DACL.
// Never interpret a writable Windows file as POSIX world-writable or skip ACLs.
const WRITE_RIGHTS = 2 | 4 | 16 | 64 | 256 | 65536 | 262144 | 524288 | 0x10000000 | 0x40000000;
const TRUSTED_INSTALLER = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464';
function validateWindowsAcl(value) {
  if (!value || !/^S-1-5-/.test(value.currentSid) || value.reparse === true
      || !Array.isArray(value.access) || !value.access.length) throw new Error('Unsafe file permissions');
  const trusted = new Set([value.currentSid, 'S-1-5-18', 'S-1-5-32-544', TRUSTED_INSTALLER]);
  if (!trusted.has(value.owner)) throw new Error('Unsafe file owner');
  for (const entry of value.access) {
    if (!entry || typeof entry.sid !== 'string' || typeof entry.allow !== 'boolean'
        || !Number.isSafeInteger(entry.rights)) throw new Error('Invalid file permissions');
    // Deliberately conservative: do not try to subtract deny ACEs from grants.
    if (entry.allow && (entry.rights & WRITE_RIGHTS) && !trusted.has(entry.sid)) throw new Error('Unsafe file permissions');
  }
}

function aclScript(filename, protect) {
  const encodedPath = Buffer.from(filename, 'utf16le').toString('base64');
  return `$ErrorActionPreference='Stop'
$p=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('${encodedPath}'))
$item=Get-Item -LiteralPath $p -Force
if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Unsafe file' }
$current=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trusted=@($current,'S-1-5-18','S-1-5-32-544','${TRUSTED_INSTALLER}')
$acl=Get-Acl -LiteralPath $p
$owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
if ($trusted -notcontains $owner) { throw 'Unsafe owner' }
$parent=Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($p)) -Force
if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Unsafe parent' }
$parentAcl=Get-Acl -LiteralPath $parent.FullName
if ($trusted -notcontains $parentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value) { throw 'Unsafe parent owner' }
$parentRules=@($parentAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
if ($parentRules.Count -eq 0) { throw 'Unsafe parent permissions' }
foreach($rule in $parentRules) {
  if (($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) { continue }
  if ($rule.AccessControlType -eq 'Allow' -and ([long]$rule.FileSystemRights -band ${WRITE_RIGHTS}) -ne 0 -and $trusted -notcontains $rule.IdentityReference.Value) { throw 'Unsafe parent permissions' }
}
${protect ? `$acl.SetAccessRuleProtection($true,$false)
foreach($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
foreach($sid in @($current,'S-1-5-18','S-1-5-32-544')) {
  $identity=[Security.Principal.SecurityIdentifier]::new($sid)
  if ($item.PSIsContainer) {
    $rule=[Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
  } else {
    $rule=[Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','Allow')
  }
  [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $p -AclObject $acl
$acl=Get-Acl -LiteralPath $p` : ''}
$access=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
  @{sid=$_.IdentityReference.Value; rights=[long]$_.FileSystemRights; allow=($_.AccessControlType -eq 'Allow')}
})
@{currentSid=$current; owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; reparse=$false; access=$access} | ConvertTo-Json -Depth 4 -Compress`;
}

function createFileSecurity({ platform = process.platform, run = promisify(execFile), runSync = execFileSync,
  systemRoot = process.env.SystemRoot || 'C:\\Windows' } = {}) {
  function aclInvocation(filename, protect = false) {
    const executable = path.win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    const command = Buffer.from(aclScript(filename, protect), 'utf16le').toString('base64');
    // No shell, profiles, user-supplied code, or unbounded process/output.
    return [executable, ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
      { windowsHide: true, timeout: 10000, maxBuffer: 65536, encoding: 'utf8' }];
  }
  async function windowsAcl(filename, protect = false) {
    const result = await run(...aclInvocation(filename, protect));
    validateWindowsAcl(JSON.parse(result.stdout.replace(/^\uFEFF/, '')));
  }
  return Object.freeze({
    assertSafeSync(filename, stat) {
      if (stat.isSymbolicLink()) throw new Error('Unsafe file');
      if (platform === 'win32') {
        const stdout = runSync(...aclInvocation(filename));
        validateWindowsAcl(JSON.parse(stdout.replace(/^\uFEFF/, '')));
      } else if (stat.mode & 0o022) throw new Error('Unsafe file permissions');
    },
    async assertSafe(filename, stat) {
      if (stat.isSymbolicLink()) throw new Error('Unsafe file');
      if (platform === 'win32') await windowsAcl(filename);
      else if (stat.mode & 0o022) throw new Error('Unsafe file permissions');
    },
    async protectPrivate(filename, mode) {
      const stat = await fs.lstat(filename);
      if (stat.isSymbolicLink() || (!stat.isFile() && !stat.isDirectory())) throw new Error('Unsafe file');
      if (platform === 'win32') await windowsAcl(filename, true);
      else await fs.chmod(filename, mode);
    },
  });
}

module.exports = { ...createFileSecurity(), createFileSecurity, validateWindowsAcl };
