export interface QueryEntry {
	collection: string;
	operation: string;
	args: unknown;
	result?: unknown;
	result_count?: number;
	duration_ms: number;
	ts: number;
	endpoint: string;
}

class QueryLogStore {
	entries = $state<QueryEntry[]>([]);

	/** Record the queries from one API call, in execution order (a page's
	 *  primary read shows first / on top). Replaces any prior entries from the
	 *  SAME route (path without query string) so repeated calls — filter changes,
	 *  Load more, refetches — don't pile up; the inspector shows one entry per
	 *  query the view actually runs. */
	push(queries: QueryEntry[], endpoint: string) {
		const route = (e: string) => e.split('?')[0];
		const here = route(endpoint);
		const enriched = queries.map((q) => ({ ...q, endpoint }));
		const others = this.entries.filter((e) => route(e.endpoint) !== here);
		// Collapse identical queries to a single card. The consent gate runs the
		// SAME customer_profiles.find_one (consent-scope resolution) on every
		// Path-B route, so a page that fires two gated calls (e.g. the txn list +
		// the money-in/out aggregate) would otherwise show it twice. Keep the
		// first occurrence so it stays at the top in execution order.
		const seen = new Set<string>();
		const deduped: QueryEntry[] = [];
		for (const e of [...others, ...enriched]) {
			const sig = `${e.collection}|${e.operation}|${JSON.stringify(e.args)}`;
			if (seen.has(sig)) continue;
			seen.add(sig);
			deduped.push(e);
		}
		this.entries = deduped.slice(-50);
	}

	clear() {
		this.entries = [];
	}

	get count() {
		return this.entries.length;
	}
}

export const queryLog = new QueryLogStore();
