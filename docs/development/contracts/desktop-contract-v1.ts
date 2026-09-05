/** Public IPC types now live with the application; this file remains the documentation entry. */
export type * from '../../../frontend/shared/desktop-contract';
import type { MaterialsQuery, MaterialsPage } from '../../../frontend/shared/desktop-contract';

// Synthetic type examples; no credentials, preload registration or network calls.
export const exampleQuery = { limit: 20, offset: 0 } as const satisfies MaterialsQuery;
export const exampleEmptyPage = {
  items: [], total: 0, limit: 20, offset: 0, hasMore: false,
} as const satisfies MaterialsPage;
