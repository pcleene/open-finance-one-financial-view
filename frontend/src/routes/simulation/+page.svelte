<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, fmtRM } from '$lib/api';
	import HowItWorks from '$lib/HowItWorks.svelte';

	let count = $state(200);
	let concurrency = $state(8);
	let mode = $state('incremental');
	let runId = $state('');
	let run: any = $state(null);
	let history: any[] = $state([]);
	let firing = $state(false);
	let showHow = $state(false);
	let poll: ReturnType<typeof setInterval>;

	// "How it works" — the incremental re-pull, end-to-end, but across a whole
	// COHORT instead of one customer: select every authorized consent, fan out
	// concurrently through the per-DP rate budget, and idempotently top-up only
	// what's new since each account's last sync.
	const INCREMENTAL_FLOW = [
		{
			title: 'Select the authorized cohort',
			tone: 'mongo',
			api: { method: 'POST', path: '/api/ops/simulation' },
			file: 'services/simulation_service.py · start(mode="incremental")',
			desc: 'Unlike Manage Profile (one customer), the simulator targets the whole cohort: every authorized, unexpired consent that can read transactions, capped at the chosen size (50 / 200 / 500 / 1000). One run drives them all.',
			code: `db.consents.find({
  status: "authorized",
  permissions: "read_transactions",
  expiration_datetime: { $gt: now },        // unexpired only
  customer_id: { $ne: null }
}).limit(count)                              // the cohort
// → insert simulation_runs { status:"running", found, consents_done:0, ... }
// no candidates → 400 "link/seed first"`
		},
		{
			title: 'Fan out across the cohort, rate-bounded',
			tone: 'logic',
			file: 'services/simulation_service.py · _run()',
			desc: 'Every consent is processed concurrently behind a semaphore (the "concurrency / in-flight pulls" knob), so the run measures real throughput under load. Each consent goes through the same incremental_sync_consent the scheduler would call.',
			code: `sem = asyncio.Semaphore(concurrency)         // 4 / 8 / 16 / 32 in-flight
await asyncio.gather(*(one(c) for c in candidates))   // all consents at once

async def one(c):
  async with sem:
    res = await incremental_sync_consent(db, c)        // per-consent (next steps)
    totals["consents_done"] += 1
    totals["transactions"] += res.transactions         // + calls / 429s / latency`,
			note: 'Idempotent upserts downstream (step 5) mean the entire cohort is safe to re-run — no duplicate rows.'
		},
		{
			title: 'Per consent: compute the incremental watermark',
			tone: 'mongo',
			file: 'ingestion/sync.py · incremental_sync_consent()',
			desc: 'For each account inside the consent scope, the pull starts from that account\u2019s last-pulled date minus a small overlap window — a top-up, not a full re-backfill.',
			code: `for acc in profile.accounts where acc.account_id in consent.accounts:  // consent-scoped
  last = acc.sync.last_txn_date_pulled               // per-account watermark
  from_date = (last ?? now-180d) - overlap_days(2)   // re-pull a 2-day overlap
  n, _, newest = await pull_transactions(db, client, consent, cid, acc, from_date)`
		},
		{
			title: 'Pull from the OFP — 200 req/min, cursor-paged, 429 backoff',
			tone: 'api',
			api: { method: 'GET', path: '/v1/accounts/{id}/transactions' },
			file: 'ingestion/ofp_client.py · OFPClient.get() · PerDPRateLimiter',
			desc: 'The spec-faithful consumption path: a per-Data-Provider rolling-60s token bucket (200 req/min), cursor pagination replayed verbatim, and 429 handling. Run across a cohort, this is where the rate limiter and retries actually bite — visible as 429s and p50/p99 in the tiles.',
			code: `await limiter.acquire(dp_id)        // PerDPRateLimiter: 200 req/min, rolling 60s
GET /v1/accounts/{id}/transactions?from_booking_date=...&cursor=...   // cursor paged
// 429 → wait Retry-After (else 5/10/20/40s + jitter, max 5 attempts) then retry
// BACKOFF_SCALE shrinks the waits for a live demo without changing their shape`
		},
		{
			title: 'Enrich + idempotent upsert',
			tone: 'mongo',
			file: 'ingestion/backfill.py · pull_transactions()',
			desc: 'Each row is enriched at write time and upserted on a deterministic _id, so re-pulling the overlap window (or re-running the whole cohort) never creates duplicates.',
			code: `_id = \`\${dp_id}::\${txn.transaction_id}\`            // deterministic → idempotent
enrichment = categorize(txn)        // category / merchant / channel / temporal / tags
bulk += UpdateOne({ _id }, { $set: { ...txn, enrichment, consent_id } }, upsert=true)
db.transactions.bulk_write(bulk, ordered=false)`
		},
		{
			title: 'Advance the watermark + pull ledger',
			tone: 'mongo',
			file: 'ingestion/sync.py (finally) · ofp_pull_ledger',
			desc: 'After each account, the watermark moves forward so the next sync stays incremental, and a per-pull ledger row records the calls / 429s / latency / rows that feed the run tiles. If anything new landed, the recurring detector re-runs.',
			code: `db.customer_profiles.update_one(
  { _id: cid, "accounts.account_id": acc.account_id },
  { $set: { "accounts.$.sync.last_txn_date_pulled": newest,
            "accounts.$.sync.last_full_sync_at": now } })

db.ofp_pull_ledger.insert_one({ kind:"incremental", consent_id, dp_id,
  calls, retries_429, duration_ms, transactions })   // → run tiles
if total: await detect_recurring(db, customer_id)`
		},
		{
			title: 'Live cohort progress',
			tone: 'logic',
			api: { method: 'GET', path: '/api/ops/simulation/{run_id}' },
			file: 'services/simulation_service.py · _persist()',
			desc: 'Every 5 consents the run doc is updated with rolling counters and percentiles; this page polls it every 1.5s to drive the progress bar (consents_done / found) and the live tiles (txns, tps, p50/p99, 429s).',
			code: `db.simulation_runs.update_one({ _id: run_id }, { $set: {
  consents_done, transactions, calls, retries_429,
  pull_p50_ms, pull_p99_ms, throughput_tps, elapsed_ms, status } })

// page polls GET /api/ops/simulation/{run_id} every 1.5s → bar + tiles`
		}
	];

	async function loadHistory() {
		history = (await api('/ops/simulation').catch(() => ({ runs: [] }))).runs ?? [];
	}
	async function refresh() {
		if (runId) run = await api(`/ops/simulation/${runId}`).catch(() => run);
	}

	onMount(() => {
		loadHistory();
		poll = setInterval(async () => {
			await refresh();
			if (run?.status === 'completed' || run?.status === 'failed') loadHistory();
		}, 1500);
	});
	onDestroy(() => clearInterval(poll));

	async function fire() {
		firing = true;
		try {
			const d = await api('/ops/simulation', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ count, concurrency, mode })
			});
			runId = d.run_id;
			run = null;
			await refresh();
		} catch (e: any) {
			/* surfaced via run staying null */
		}
		firing = false;
	}

	function openRun(r: any) {
		runId = r._id;
		run = r;
	}

	const pct = $derived(run && run.found ? Math.min(100, Math.round((run.consents_done / run.found) * 100)) : 0);
	const live = $derived(run?.status === 'running');
	const isReauth = $derived(
		run?.kind === 'reauth_sim' || run?.kind === 'reseed_sim' ||
		run?.mode === 'reauthorize' || run?.mode === 'reseed'
	);
	function dur(ms: number) {
		if (!ms) return '0s';
		const s = ms / 1000;
		return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
	}
</script>

<div class="page">
	<div class="staffbadge mono">Acme staff view · internal tooling</div>

	<div class="grid" style="grid-template-columns: 1fr 1.6fr; align-items: start; gap: 18px">
		<section class="card">
			<div class="cardhead">
				<h3>Transaction ingestion simulation</h3>
				{#if mode === 'incremental'}
					<button class="howbtn" onclick={() => (showHow = true)}>How it works</button>
				{/if}
			</div>
			<div class="modes">
				<button class="seg" class:on={mode === 'incremental'} disabled={firing || live}
					onclick={() => (mode = 'incremental')}>Incremental re-pull</button>
				<button class="seg" class:on={mode === 'reauthorize'} disabled={firing || live}
					onclick={() => (mode = 'reauthorize')}>New consent (revoke + re-link)</button>
				<button class="seg" class:on={mode === 'reseed'} disabled={firing || live}
					onclick={() => (mode = 'reseed')}>Reseed (re-link revoked)</button>
			</div>
			{#if mode === 'incremental'}
				<p class="hint">
					Drives the real the Open Finance platform consumption path — <code>incremental_sync_consent</code> per
					authorized consent, through the per-DP <b>200 req/min</b> token bucket, cursor
					pagination and 429 backoff — across a cohort. Idempotent upserts, so it's safe to
					re-run. Requires the mock OFP (:8100) to be up.
				</p>
			{:else if mode === 'reauthorize'}
				<p class="hint">
					Fires <b>brand-new consents</b> through the full lifecycle: <code>revoke</code> →
					worker erases the now-uncovered accounts → <code>re-link</code> (PAR → authorize →
					token) → worker <code>backfill</code>, which <b>reconstructs the balance time series
					from the freshly pulled transactions</b> (no synthetic walk). Requires the mock OFP
					(:8100) and the eraser/backfill worker to be running.
				</p>
			{:else}
				<p class="hint">
					<b>Replenishes data the storm erased.</b> Re-links <b>revoked</b> cohort consents
					through the real flow (PAR → authorize → token → <code>backfill</code>) — no revoke,
					no erase. Mints fresh URL-safe consent_ids, so the revocation storm can revoke them
					afterwards. Requires the mock OFP (:8100) and the worker.
				</p>
			{/if}
			<label>Consents to drive
				<select bind:value={count}>
					<option value={50}>50</option>
					<option value={200}>200</option>
					<option value={500}>500</option>
					<option value={1000}>1000</option>
				</select>
			</label>
			<label>Concurrency (in-flight pulls)
				<select bind:value={concurrency}>
					<option value={4}>4</option>
					<option value={8}>8</option>
					<option value={16}>16</option>
					<option value={32}>32</option>
				</select>
			</label>
			<button class="btn primary" style="width:100%; margin-top:14px" disabled={firing || live} onclick={fire}>
				{firing ? 'starting…' : live ? 'running…' : 'FIRE simulation'}
			</button>
		</section>

		<section class="card">
			<h3>Live run {#if run}<span class="cid mono">{run._id}</span>{/if}</h3>
			{#if !run}
				<div class="mono dim">No active run — fire a simulation or open one from history.</div>
			{:else}
				<div class="progress">
					<div class="bar"><div class="fill" class:done={!live} style="width:{pct}%"></div></div>
					<div class="prog mono">
						<span class:livedot={live}>{run.status}</span>
						· {run.consents_done}/{run.found} {isReauth ? 'backfills' : 'consents'} · {pct}%
					</div>
				</div>
				{#if isReauth}
					<div class="tiles">
						<div class="tile"><span>Consents revoked</span><b class="money">{fmtRM(run.consents_revoked ?? 0, 0)}</b></div>
						<div class="tile"><span>Consents re-linked</span><b class="money">{fmtRM(run.consents_relinked ?? 0, 0)}</b></div>
						<div class="tile"><span>Backfills done</span><b class="money">{fmtRM(run.backfills_done ?? 0, 0)}</b></div>
						<div class="tile"><span>Snapshots rebuilt</span><b class="money">{fmtRM(run.snapshots_rebuilt ?? 0, 0)}</b></div>
						<div class="tile"><span>Docs erased</span><b class="money">{fmtRM(run.docs_erased ?? 0, 0)}</b></div>
						<div class="tile"><span>Txns re-ingested · errors</span><b class="money">{fmtRM(run.transactions ?? 0, 0)} · {run.errors ?? 0}</b></div>
						<div class="tile"><span>OFP calls</span><b class="money">{fmtRM(run.calls ?? 0, 0)}</b></div>
						<div class="tile"><span>Throughput</span><b class="money">{run.throughput_tps ?? '—'} <small>tps</small></b></div>
						<div class="tile"><span>Elapsed</span><b class="money">{dur(run.elapsed_ms ?? 0)}</b></div>
					</div>
				{:else}
					<div class="tiles">
						<div class="tile"><span>Transactions ingested</span><b class="money">{fmtRM(run.transactions ?? 0, 0)}</b></div>
						<div class="tile"><span>OFP calls</span><b class="money">{fmtRM(run.calls ?? 0, 0)}</b></div>
						<div class="tile"><span>429 retries</span><b class="money" class:warn={(run.retries_429 ?? 0) > 0}>{fmtRM(run.retries_429 ?? 0, 0)}</b></div>
						<div class="tile"><span>Throughput</span><b class="money">{run.throughput_tps ?? '—'} <small>tps</small></b></div>
						<div class="tile"><span>Pull p50 / p99</span><b class="money">{run.pull_p50_ms ?? '—'} / {run.pull_p99_ms ?? '—'} <small>ms</small></b></div>
						<div class="tile"><span>Elapsed · errors</span><b class="money">{dur(run.elapsed_ms ?? 0)} · {run.errors ?? 0}</b></div>
					</div>
				{/if}
			{/if}
		</section>
	</div>

	<section class="card" style="margin-top:18px">
		<h3>Run history</h3>
		{#if !history.length}
			<div class="mono dim">No simulation runs yet.</div>
		{:else}
			<table class="data">
				<thead>
					<tr><th>Run</th><th>When</th><th>Status</th><th style="text-align:right">Consents</th><th style="text-align:right">Txns</th><th style="text-align:right">Calls</th><th style="text-align:right">429s</th><th style="text-align:right">p50/p99 ms</th><th style="text-align:right">tps</th><th></th></tr>
				</thead>
				<tbody>
					{#each history as r}
						<tr class:active={r._id === runId}>
							<td class="mono dim">{r._id}</td>
							<td class="mono dim">{new Date(r.started_at).toLocaleString('en-MY', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}</td>
							<td><span class="chip {r.status === 'completed' ? 'ok' : r.status === 'failed' ? 'bad' : 'warn'}">{r.status}</span></td>
							<td class="mono" style="text-align:right">{r.consents_done}/{r.found}</td>
							<td class="mono" style="text-align:right">{fmtRM(r.transactions ?? 0, 0)}</td>
							<td class="mono" style="text-align:right">{fmtRM(r.calls ?? 0, 0)}</td>
							<td class="mono" style="text-align:right">{r.retries_429 ?? 0}</td>
							<td class="mono dim" style="text-align:right">{r.pull_p50_ms ?? '—'}/{r.pull_p99_ms ?? '—'}</td>
							<td class="mono" style="text-align:right">{r.throughput_tps ?? '—'}</td>
							<td style="text-align:right"><button class="btn ghost" onclick={() => openRun(r)}>view</button></td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</section>

	{#if showHow}
		<HowItWorks
			title="Incremental re-pull — how it works"
			subtitle="The scheduled the Open Finance platform top-up, run across a whole cohort at once (not a single customer): select every authorized consent, fan out concurrently through the per-DP 200 req/min budget, and idempotently upsert only what's new since each account's last sync. Expand any step for the API + query."
			footer="Same consumption path as production's scheduled sync — just driven across N consents on demand so you can watch throughput, 429 backoff and p50/p99 live. Idempotent upserts (_id = dp_id::transaction_id) make the whole cohort safe to re-run."
			steps={INCREMENTAL_FLOW}
			onclose={() => (showHow = false)}
		/>
	{/if}
</div>

<style>
	.staffbadge {
		display: inline-block; font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase;
		color: var(--amber); border: 1px solid color-mix(in srgb, var(--amber) 35%, transparent);
		background: color-mix(in srgb, var(--amber) 8%, transparent);
		padding: 4px 10px; border-radius: 7px; margin-bottom: 16px;
	}
	.cardhead { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
	.cardhead h3 { margin-bottom: 0; }
	.howbtn { background: transparent; border: 1px solid var(--line-2); color: var(--ink-dim);
		font-family: var(--sans); font-size: 10.5px; font-weight: 600; padding: 4px 10px;
		border-radius: 999px; cursor: pointer; transition: all 0.15s; white-space: nowrap; }
	.howbtn:hover { color: var(--ink); border-color: var(--ink-faint); }
	.hint { font-size: 12px; color: var(--ink-dim); line-height: 1.55; margin: 0 0 14px; }
	.modes { display: flex; gap: 0; margin: 4px 0 14px; border: 1px solid var(--line); border-radius: 9px; overflow: hidden; }
	.seg { flex: 1; padding: 8px 10px; font-size: 11.5px; background: transparent; color: var(--ink-dim); border: none; cursor: pointer; border-right: 1px solid var(--line); }
	.seg:last-child { border-right: none; }
	.seg:hover:not(:disabled) { background: rgba(255,255,255,0.03); }
	.seg.on { background: color-mix(in srgb, var(--amber) 14%, transparent); color: var(--ink); font-weight: 600; }
	.seg:disabled { opacity: 0.55; cursor: default; }
	label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--ink-faint); margin-top: 12px; }
	label select { display: block; width: 100%; margin-top: 5px; }
	.cid { font-size: 10px; color: var(--ink-faint); margin-left: 8px; }
	.dim { color: var(--ink-faint); font-size: 11.5px; }

	.progress { margin: 6px 0 16px; }
	.bar { height: 10px; border-radius: 6px; background: var(--bg-raise, rgba(255,255,255,0.05)); overflow: hidden; border: 1px solid var(--line); }
	.fill { height: 100%; background: linear-gradient(90deg, var(--green), rgba(62,207,142,0.5)); transition: width 0.5s ease; }
	.fill.done { background: linear-gradient(90deg, var(--green), var(--green)); }
	.prog { font-size: 11px; color: var(--ink-dim); margin-top: 8px; }
	.livedot::before { content: '●'; color: var(--green); margin-right: 5px; animation: pulse 1.3s infinite; }
	@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

	.tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
	.tile { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; background: rgba(255,255,255,0.015); }
	.tile span { display: block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-faint); margin-bottom: 6px; }
	.tile b { font-size: 20px; }
	.tile b small { font-size: 11px; color: var(--ink-faint); }
	.tile b.warn { color: var(--amber); }

	tr.active { background: rgba(62,207,142,0.06); }
</style>
