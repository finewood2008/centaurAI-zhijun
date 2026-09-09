import type { ProductDesktop } from './product-contract';
/** Public, credential-free desktop IPC types. Runtime validation lives in shell/runtime. */
export type Generation = number;

export interface CallContext {
  readonly callId: string;
  readonly expectedGeneration: Generation;
}

export type PublicErrorCode =
  | 'CONFIGURATION_REQUIRED'
  | 'AUTHENTICATION_REQUIRED'
  | 'AUTHENTICATION_FAILED'
  | 'APPLICATION_AUTHORIZATION_DENIED'
  | 'ACCOUNT_SERVICE_UNAVAILABLE'
  | 'SECURE_STORAGE_UNAVAILABLE'
  | 'BUSINESS_BRIDGE_REQUIRED'
  | 'SESSION_NOT_READY'
  | 'STALE_GENERATION'
  | 'INVALID_REQUEST'
  | 'CLAIM_CODE_INVALID'
  | 'CLAIM_CODE_EXPIRED'
  | 'DEVICE_ALREADY_CLAIMED'
  | 'OPERATION_NOT_ALLOWED'
  | 'ACCESS_DENIED'
  | 'SESSION_EXPIRED'
  | 'CONNECTIVITY_SESSION_EXPIRED'
  | 'TRANSPORT_UNAVAILABLE'
  | 'DIRECT_CONNECTION_UNAVAILABLE'
  | 'WRITE_OUTCOME_UNKNOWN'
  | 'REQUEST_TIMEOUT'
  | 'RESOURCE_EXHAUSTED'
  | 'RATE_LIMITED'
  | 'SESSION_QUOTA_EXHAUSTED'
  | 'CONTRACT_MISMATCH'
  | 'RESPONSE_TOO_LARGE'
  | 'REMOTE_ERROR'
  | 'READ_CANCELLED';

export interface PublicError {
  readonly code: PublicErrorCode;
  /** Sanitized application message, never a raw exception or response body. */
  readonly message: string;
  readonly httpStatus?: number;
  readonly remoteCode?: string;
  readonly traceId?: string;
  /** Main-process fixed allowlists only; no raw URLs, tickets, bodies or messages. */
  readonly phase?: 'account_service' | 'ticket' | 'native';
  readonly sdkCode?: string;
  readonly detailCode?: 'DIRECT_TIMEOUT' | 'ICE_FAILED';
  /** A user action hint, not permission for automatic replay. */
  readonly recovery: 'none' | 'user_read' | 'user_reconnect' | 'user_sign_in';
}

export type Result<T> =
  | Readonly<{ ok: true; generation: Generation; data: T }>
  | Readonly<{ ok: false; generation: Generation; error: PublicError }>;

export type Phase = 'signed_out' | 'authenticating' | 'selecting_device'
  | 'connecting' | 'authorizing' | 'ready' | 'disconnecting' | 'failed';

export interface M0Capabilities {
  readonly materialsRead: boolean;
  readonly streamChat: boolean;
  readonly product: boolean;
  readonly uploads: boolean;
  readonly matters: boolean;
  readonly provisioning: false;
}

interface SnapshotBase {
  /** Authoritative host mode; simulation never proves a real device connection. */
  readonly environment: 'unconfigured' | 'simulation' | 'production';
  readonly protocolVersion: 1;
  readonly generation: Generation;
  /** Monotonic across all generations within one main-process lifetime. */
  readonly sequence: number;
}

export type DesktopSnapshot =
  | (SnapshotBase & Readonly<{
      phase: 'ready';
      subject: Readonly<{ accountId: string; deviceId: string; deviceName?: string; workspaceId?: string }>;
      capabilities: M0Capabilities & Readonly<{ materialsRead: true }>;
    }>)
  | (SnapshotBase & Readonly<{
      phase: Exclude<Phase, 'ready'>;
      subject: Readonly<{ accountId: string; deviceId?: string; deviceName?: string; workspaceId?: string }> | null;
      capabilities: M0Capabilities & Readonly<{ materialsRead: false }>;
      error?: PublicError;
    }>);

export interface DeviceSummary {
  readonly deviceId: string;
  readonly displayName: string;
  readonly availability: 'online' | 'offline' | 'unknown';
}

export type MaterialType = 'document' | 'image' | 'audio';
export type MaterialStatus = 'uploaded' | 'queued' | 'processing' | 'available' | 'failed';

export interface MaterialsQuery {
  /** Integer, 1..50; default chosen by the page is 20. */
  readonly limit: number;
  /** Integer, 0..10000. */
  readonly offset: number;
  readonly keyword?: string;
  readonly type?: MaterialType;
  readonly status?: MaterialStatus;
}

export interface MaterialSummary {
  readonly materialId: string;
  readonly fileName: string;
  readonly fileType: MaterialType;
  readonly status: MaterialStatus;
  readonly createdAt: string;
  // No folder/folderId, host paths, body text or previewUrl in M0.
}

export interface MaterialsPage {
  readonly items: readonly MaterialSummary[];
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
  readonly hasMore: boolean;
}

export interface PasswordCredentials {
  readonly phone: string;
  readonly password: string;
  readonly rememberPassword: boolean;
}

export interface RegistrationCredentials extends PasswordCredentials {
  readonly code: string;
}

export interface RegistrationCodeReceipt {
  readonly expiresIn: number;
}

export interface RememberedLogin {
  readonly phone: string;
  readonly passwordSaved: boolean;
}

export interface ZhijunDesktopV1 {
  readonly protocolVersion: 1;
  getSnapshot(): Promise<Result<DesktopSnapshot>>;
  /** Returns only the account hint and whether an encrypted password exists. */
  getRememberedLogin(context: CallContext): Promise<Result<RememberedLogin | null>>;
  /** Preload strips Electron events and returns a local unsubscribe function. */
  subscribe(listener: (snapshot: DesktopSnapshot) => void): () => void;
  beginSignIn(context: CallContext): Promise<Result<DesktopSnapshot>>;
  /** Explicit user input only; main may encrypt it when rememberPassword is true. No tokens cross IPC. */
  signInWithPassword(context: CallContext, credentials: PasswordCredentials): Promise<Result<DesktopSnapshot>>;
  /** The decrypted password stays in the main process. This never signs in automatically. */
  signInWithSavedPassword(context: CallContext, rememberPassword: boolean): Promise<Result<DesktopSnapshot>>;
  /** Sends an SMS registration proof. Debug codes and provider details never cross IPC. */
  sendRegistrationCode(context: CallContext, phone: string): Promise<Result<RegistrationCodeReceipt>>;
  /** Registers the Consumer account and enters the authenticated device-selection state. */
  registerWithPassword(context: CallContext, credentials: RegistrationCredentials): Promise<Result<DesktopSnapshot>>;
  listDevices(context: CallContext): Promise<Result<readonly DeviceSummary[]>>;
  /** Redeems a user-entered Admin claim code for the authenticated account. */
  claimDevice(context: CallContext, claimToken: string): Promise<Result<DeviceSummary>>;
  connect(context: CallContext, deviceId: string): Promise<Result<DesktopSnapshot>>;
  disconnect(context: CallContext): Promise<Result<DesktopSnapshot>>;
  signOut(context: CallContext): Promise<Result<DesktopSnapshot>>;
  readonly product: ProductDesktop;
  readonly materials: Readonly<{
    list(context: CallContext, query: MaterialsQuery): Promise<Result<MaterialsPage>>;
  }>;
  /** Suppresses local delivery only; no claim of native/server cancellation. */
  cancelRead(context: CallContext, targetCallId: string): Promise<Result<
    Readonly<{ delivery: 'suppressed' | 'not_found'; remoteCancellation: 'not_supported' }>
  >>;
}
