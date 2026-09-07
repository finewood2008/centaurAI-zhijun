import type { DesktopSnapshot } from '../../../shared/desktop-contract'

type DeviceSubject = DesktopSnapshot['subject']

/** Prefer the Admin device name; legacy/empty snapshots fall back to the stable device ID. */
export function connectedDeviceLabel(subject: DeviceSubject): string {
  return subject?.deviceName?.trim() || subject?.deviceId || ''
}
