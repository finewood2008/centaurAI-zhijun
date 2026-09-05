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
  | 'SECURE_STORAGE_UNAVAILABLE'
  | 'BUSINESS_BRIDGE_REQUIRED'
  | 'SESSION_NOT_READY'
  | 'STALE_GENERATION'
  | 'INVALID_REQUEST'
  | 'OPERATION_NOT_ALLOWED'
  | 'ACCESS_DENIED'
  | 'SESSION_EXPIRED'
  | 'TRANSPORT_UNAVAILABLE'
  | 'REQUEST_TIMEOUT'
  | 'RESOURCE_EXHAUSTED'
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
  readonly streamChat: false;
  readonly uploads: false;
  readonly matters: false;
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
      subject: Readonly<{ accountId: string; deviceId: string }>;
      capabilities: M0Capabilities & Readonly<{ materialsRead: true }>;
    }>)
  | (SnapshotBase & Readonly<{
      phase: Exclude<Phase, 'ready'>;
      subject: Readonly<{ accountId: string; deviceId?: string }> | null;
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
}

export interface ZhijunDesktopV1 {
  readonly protocolVersion: 1;
  getSnapshot(): Promise<Result<DesktopSnapshot>>;
  /** Preload strips Electron events and returns a local unsubscribe function. */
  subscribe(listener: (snapshot: DesktopSnapshot) => void): () => void;
  beginSignIn(context: CallContext): Promise<Result<DesktopSnapshot>>;
  /** One-time user input only; no access/refresh tokens cross IPC. */
  signInWithPassword(context: CallContext, credentials: PasswordCredentials): Promise<Result<DesktopSnapshot>>;
  listDevices(context: CallContext): Promise<Result<readonly DeviceSummary[]>>;
  connect(context: CallContext, deviceId: string): Promise<Result<DesktopSnapshot>>;
  disconnect(context: CallContext): Promise<Result<DesktopSnapshot>>;
  signOut(context: CallContext): Promise<Result<DesktopSnapshot>>;
  readonly materials: Readonly<{
    list(context: CallContext, query: MaterialsQuery): Promise<Result<MaterialsPage>>;
  }>;
  /** Suppresses local delivery only; no claim of native/server cancellation. */
  cancelRead(context: CallContext, targetCallId: string): Promise<Result<
    Readonly<{ delivery: 'suppressed' | 'not_found'; remoteCancellation: 'not_supported' }>
  >>;
}

