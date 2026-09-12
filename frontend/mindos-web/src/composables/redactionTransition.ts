const DETAIL_REFRESH_STATES = new Set(['ready', 'pending_summary'])

/**
 * The initial status read only hydrates the panel. Reloading the parent on that
 * read would unmount the panel and start the same read again forever.
 */
export function shouldRefreshDetailAfterRedactionTransition(
  previous: string | undefined,
  next: string,
): boolean {
  return previous !== undefined && previous !== next && DETAIL_REFRESH_STATES.has(next)
}
