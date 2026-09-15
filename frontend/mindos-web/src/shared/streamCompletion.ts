/** In-process marker only: a parsed domain terminal event is not user cancellation.
 * Never serialize it or accept an equivalent string/object from a remote response.
 */
export const COMPLETED_SSE_STREAM = Symbol('completed-sse-stream')
