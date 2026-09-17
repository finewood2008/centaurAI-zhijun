'use strict';
module.exports = async context => {
  if (context.electronPlatformName !== 'win32') throw new Error('PACKAGE_PLATFORM_UNSUPPORTED');
  await require('./verify-windows-package.cjs').verifyWindowsApp(context.appOutDir, {
    unsigned: process.env.ZHIJUN_WINDOWS_UNSIGNED === '1', afterPack: true,
  });
};
