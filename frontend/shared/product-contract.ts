import type { CallContext, Result } from './desktop-contract';

export type ProductState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
export interface ProductOperationRequest {
  readonly version: 1;
  readonly requestId: string;
  readonly operationId: string;
  readonly params: Readonly<Record<string, string>>;
  readonly query: Readonly<Record<string, string | number | boolean>>;
  readonly body: unknown;
}
export interface ProductStart { readonly id: string; readonly state: ProductState; readonly cursor: number }
export interface ProductBlob {
  readonly id: string; readonly size: number; readonly sha256: string;
  readonly contentType: string; readonly fileName?: string;
}
export type ProductEvent =
  | Readonly<{seq: number; kind: 'headers'; status: number; headers: Readonly<Record<string, string>>}>
  | Readonly<{seq: number; kind: 'chunk'; data: Uint8Array}>
  | Readonly<{seq: number; kind: 'blob'; blob: ProductBlob}>
  | Readonly<{seq: number; kind: 'end'}>
  | Readonly<{seq: number; kind: 'error'; code: string; message: string}>;
export interface ProductPoll extends ProductStart {
  readonly events: readonly ProductEvent[]; readonly hasMore: boolean;
}
export interface ProductCancel { readonly id: string; readonly state: ProductState; readonly cancelRequested: boolean }
export interface ProductUpload {
  readonly id: string; readonly state: 'open' | 'complete' | 'cancelled' | 'failed';
  readonly size: number; readonly received: number; readonly nextIndex: number; readonly sha256?: string;
}
export interface ProductBlobRead extends ProductBlob {
  readonly offset: number; readonly data: Uint8Array; readonly hasMore: boolean;
}
export interface ProductDesktop {
  /** Call only from an explicit recording button; never records or probes a device. */
  requestMicrophone(context: CallContext): Promise<Result<Readonly<{allowed: boolean}>>>;
  start(context: CallContext, request: ProductOperationRequest): Promise<Result<ProductStart>>;
  poll(context: CallContext, input: Readonly<{id: string; after: number; waitMs: number}>): Promise<Result<ProductPoll>>;
  cancel(context: CallContext, input: Readonly<{id: string; requestId: string}>): Promise<Result<ProductCancel>>;
  uploadCreate(context: CallContext, input: Readonly<{requestId: string; fileName: string; contentType: string; size: number}>): Promise<Result<ProductUpload>>;
  uploadChunk(context: CallContext, input: Readonly<{id: string; index: number; bytes: Uint8Array}>): Promise<Result<ProductUpload>>;
  uploadComplete(context: CallContext, input: Readonly<{id: string}>): Promise<Result<ProductUpload>>;
  uploadStatus(context: CallContext, input: Readonly<{id: string}>): Promise<Result<ProductUpload>>;
  uploadCancel(context: CallContext, input: Readonly<{id: string}>): Promise<Result<ProductUpload>>;
  blobRead(context: CallContext, input: Readonly<{id: string; offset: number; limit: number}>): Promise<Result<ProductBlobRead>>;
  save(context: CallContext, input: Readonly<{fileName: string; contentType: string; source: Readonly<{kind:'bytes'; bytes:Uint8Array}> | Readonly<{kind:'blob'; id:string}>}>): Promise<Result<Readonly<{saved: boolean}>>>;
  openMedia(context: CallContext, request: ProductOperationRequest): Promise<Result<Readonly<{handle:string; url:string; contentType:string}>>>;
  closeMedia(context: CallContext, input: Readonly<{handle:string}>): Promise<Result<Readonly<{closed:boolean}>>>;
}
