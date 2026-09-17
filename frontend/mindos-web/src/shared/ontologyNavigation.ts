import { ref } from 'vue'

// A repeated click on /me does not cause a router transition. Signal the page
// explicitly without persisting a view preference or remounting its data.
export const ontologyOverviewRequest = ref(0)
export function requestOntologyOverview(): void {
  ontologyOverviewRequest.value++
}
