<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, API, fmtRM, SHORT_NAMES } from '$lib/api';
	import HowItWorks from '$lib/HowItWorks.svelte';

	let showHow = $state(false);

	// "How it works" — the revocation storm, end-to-end: revoke → ordered event →
	// ACID gate-flip → async chunked erasure, while reads stay flat.
	const STORM_FLOW = [
		{
			title: 'Pick the cohort to revoke',
			tone: 'mongo',
			api: { method: 'POST', path: '/api/ops/storm' },
			file: 'services/ops_service.py · storm()',
			desc: 'The storm targets N authorized consents from the synthetic cohort (customer_id ≥ acme_cust_001000) — consents the mock OFP actually knows about, so every revoke is honoured by the full pipeline rather than 404-ing.',
			code: `db.consents.find(
  { status: "authorized", customer_id: { $gte: "acme_cust_001000" } },
  { consent_id: 1 }
).limit(n)
// no candidates → 400 "reseed/relink" (Re-link cohort replenishes them)`
		},
		{
			title: 'Revoke through the real LCM endpoint',
			tone: 'api',
			api: { method: 'POST', path: '/v1/consents/{consent_id}/revoke' },
			file: 'services/ops_service.py · _run_storm() · revoke_one()',
			desc: 'N concurrent revocations (asyncio.Semaphore(40)) hit the OFP Consent LCM endpoint — exactly the call a customer tapping "revoke" would make. On 201 the post-image is handed to the single ordered consent path — which, on the deployed AWS backend, PUBLISHES TO MSK (next step).',
			code: `// concurrent, rate-bounded — the customer-facing revoke, at scale
POST /v1/consents/{consent_id}/revoke   { "updated_by": "data_consumer_user" }

// 201 → hand the authoritative post-image to the ONE ordered consent path.
// Deployed AWS backend runs CONSENT_EVENT_TRANSPORT=kafka → this produces to MSK:
await publish_consent_event(db, response.json())
// non-201 is surfaced (errors / error_samples), never swallowed`
		},
		{
			title: 'Publish to MSK — one ordered path',
			tone: 'kafka',
			file: 'consent/producer.py · publish_consent_event()',
			desc: 'The deployed AWS backend runs CONSENT_EVENT_TRANSPORT=kafka, so the post-image is PRODUCED to the rcp.consent.events MSK topic, keyed by consent_id (every event for one consent stays strictly ordered on its partition) with a monotonic _rcp_version. Local dev uses transport=direct — the identical guarded upsert, in-process. Downstream of the topic, nothing changes.',
			code: `// monotonic per consent_id — guards against out-of-order replay
doc._rcp_version = int(updated_at.timestamp() * 1000)

// DEPLOYED (AWS): transport = "kafka" → produce the post-image to MSK, keyed by
// consent_id so all events for one consent stay strictly ordered on its partition:
await get_consent_producer().publish(
    topic="rcp.consent.events", key=doc._id, value=doc)   // Extended JSON → keeps $date / $numberDecimal

// LOCAL dev: transport = "direct" → the SAME guarded upsert, in-process:
//   db.consents.replace_one({ _id, $or:[{_rcp_version:{$lt:v}},{_rcp_version:{$exists:false}}] }, doc)`,
			note: 'The front end / webhook receiver / consent centre NEVER write consent state into profiles directly — everything flows through this one path.'
		},
		{
			title: 'Kafka Connect sink → consents upsert',
			tone: 'kafka',
			file: 'ops/kafka/consents-mongo-sink.json · MongoSinkConnector',
			desc: 'The MongoDB Kafka Connect sink (running on the kafka-connect container) consumes rcp.consent.events and applies the upsert into acme_ofv.consents, keyed by the _id carried in the value (= consent_id). This is the ONLY writer of the consents collection; nothing downstream knows whether the write arrived via Kafka or the in-process default.',
			code: `// ops/kafka/consents-mongo-sink.json  (POST :8083/connectors on kafka-connect)
{
  "connector.class": "com.mongodb.kafka.connect.MongoSinkConnector",
  "topics": "rcp.consent.events",
  "database": "acme_ofv", "collection": "consents",
  "document.id.strategy": "...id.strategy.ProvidedInValueStrategy",   // _id = consent_id (from value)
  "writemodel.strategy": "...writemodel.strategy.ReplaceOneDefaultStrategy",
  "document.id.strategy.overwrite.existing": "true",
  "value.converter": "...StringConverter"            // Extended JSON → real Date / Decimal128
}
// → upserts acme_ofv.consents; the change stream below fires on that write`
		},
		{
			title: 'Change stream → worker → synchronous ACID gate-flip',
			tone: 'txn',
			file: 'eraser/worker.py · consents_watcher() + gate_flip()',
			desc: 'The eraser worker TAILS the acme_ofv.consents change stream (resume token persisted in stream_checkpoints → crash-resumable). When the sink writes the revoked post-image, the watcher fires, flips the embedded consent boxes AND writes the audit entry in one multi-document ACID transaction, then schedules erasure. From the instant that commits, every read path excludes the accounts — no matter how many rows still await deletion.',
			code: `// eraser/worker.py — the change-stream watcher that drives all of this:
async with db.consents.watch(
    [{ $match: { operationType: { $in: ["insert","replace","update"] } } }],
    resume_after=token) as stream:                 // resumable from stream_checkpoints
  async for change in stream:
    consent = change.fullDocument
    if consent.status in ("revoked","expired","suspended"):
      async with session.start_transaction():       // ACID: flip + audit commit together
        db.customer_profiles.update_one({ _id: customer_id },
          { $set: { "accounts.$[a].consents.$[c].status": consent.status } },
          arrayFilters=[{ "a.consents.consent_id": consent_id }, { "c.consent_id": consent_id }])
        db.consent_audit_log.insert_one({ action: "gate_flip", status: consent.status, flip_ms })
      if consent.status == "revoked": create_task(erase_consent_data(consent))   // → next step
    save_token("consents_watch", change._id)         // checkpoint → resumable
// flip is typically single-digit ms, independent of data volume`,
			note: 'Why profile-only? BOTH read paths derive live consent from these embedded profile boxes — One View $filters them in the read (Path A); transactions / insights / search / underwriting call resolve_consent_scope(), which reads accounts.consents to build the allowed-account set (Path B). The consents collection is the event/ordering source + audit, never the request-time gate — so flipping the box here is the single enforcement switch (no need to touch the transaction rows).'
		},
		{
			title: 'Async chunked physical erasure',
			tone: 'worker',
			file: 'eraser/worker.py · erase_consent_data() + _drive_erasure()',
			desc: 'Deletion runs after the gate flip, only for accounts no longer covered by ANY other authorized consent (set difference). Transactions are deleted in 2,000-_id batches off the covering index — no collection scans, no table locks — recording per-batch metrics + a heartbeat (updated_at) into the erasure_job. On any error the job is marked failed (never left stuck) so the sweeper can retry it.',
			code: `job = db.erasure_jobs.insert_one({ status: "running", attempts: 1, accounts: erase_set, ... })
while (true) {
  ids = db.transactions.find(
    { customer_id, "account.account_id": { $in: erase_set } },
    { _id: 1 }).limit(2000)              // covering index — bounded batch
  if (ids.empty) break
  db.transactions.delete_many({ _id: { $in: ids } })
  db.erasure_jobs.update_one({ _id: job }, {
    $inc: { docs_deleted, batches }, $currentDate: { updated_at: true } })   // heartbeat
}
db.balance_snapshots.delete_many({ "meta.customer_id": cid, "meta.account_id": { $in: erase_set } })
db.uw_features.update_one({ _id: cid }, { $pull: { accounts: { account_id: { $in: erase_set } } } })
db.erasure_jobs.update_one({ _id: job }, { $set: { status: "completed", snapshots_deleted } })
// any throw → { status: "failed", last_error } so the sweeper re-drives it`
		},
		{
			title: 'Self-healing retry — the erasure sweeper',
			tone: 'event',
			tag: 'Self-heal',
			file: 'eraser/worker.py · erasure_sweeper()',
			desc: 'Erasure is fire-and-forget off the change stream (the resume token advances once the gate-flip commits), so a worker crash or transient error mid-delete would orphan rows. The sweeper is the retry guarantee: every 60s it atomically CLAIMS any job left "running" with a stale heartbeat — i.e. the worker died mid-delete — or "failed", under an attempt ceiling, and resumes it. The delete is idempotent (it re-queries the remaining rows), so a half-finished job just finishes. No ACID transaction needed — one would blow the 16MB / 60s transaction limits and stall the read path.',
			code: `// every 60s — claim-then-drive; the atomic update IS the lease
job = db.erasure_jobs.find_one_and_update(
  { status: { $in: ["running","failed"] },
    attempts: { $lt: 5 },
    updated_at: { $lt: now - 120s } },              // stale heartbeat = worker died mid-flight
  { $set: { status: "running" }, $inc: { attempts: 1 }, $currentDate: { updated_at: true } },
  returnDocument: "after")
if (job) resume(job)   // re-queries REMAINING rows → idempotent, safe to retry to completion`
		},
		{
			title: 'Reads don’t flinch',
			tone: 'mongo',
			file: 'services/ops_service.py · _run_storm() · read_load()',
			desc: 'Throughout the storm a steady One View read load runs and we record p50 / p99 / max. The point being proven: revoke-at-scale + bulk erasure do not degrade the read path.',
			code: `// steady One View load while the storm rages — sampled into latencies[]
db.customer_profiles.aggregate( one_view_pipeline(cid, "one_view") )  // single indexed read
// p99 stays flat: gate-flip is O(1) per consent, erasure is chunked off
// the covering index, WiredTiger gives document-level concurrency.`
		}
	];

	let metrics: any = $state(null);
	let events: { label: string; doc: any; at: number }[] = $state([]);
	let stormN = $state(200);
	let stormRun: any = $state(null);
	let stormId = $state('');
	let es: EventSource | null = null;
	let poll: ReturnType<typeof setInterval>;
	let deletesTotal = $state(0);
	let history: any[] = $state([]);
	const historyLoadedFor = new Set<string>();

	async function loadHistory() {
		history = (await api('/ops/storm').catch(() => ({ runs: [] }))).runs ?? [];
	}

	let reseedId = $state('');
	let reseedRun: any = $state(null);

	async function refreshMetrics() {
		metrics = await api('/ops/metrics');
		deletesTotal = Object.values(metrics.erasure_jobs ?? {}).reduce(
			(s: number, j: any) => s + (j.docs ?? 0) + (j.snapshots ?? 0), 0);
		if (stormId) {
			stormRun = await api(`/ops/storm/${stormId}`).catch(() => stormRun);
			if (stormRun?.status === 'completed' && !historyLoadedFor.has(stormId)) {
				historyLoadedFor.add(stormId);
				loadHistory();
			}
		}
		if (reseedId) reseedRun = await api(`/ops/simulation/${reseedId}`).catch(() => reseedRun);
	}

	async function fireReseed() {
		try {
			const d = await api('/ops/simulation', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ mode: 'reseed', count: 50, concurrency: 8 })
			});
			reseedId = d.run_id;
			reseedRun = null;
		} catch { /* surfaced via reseedRun staying null */ }
	}

	onMount(() => {
		refreshMetrics();
		loadHistory();
		poll = setInterval(refreshMetrics, 2000);
		es = new EventSource(`${API}/ops/events`);
		const push = (label: string) => (e: MessageEvent) => {
			events = [{ label, doc: JSON.parse(e.data), at: Date.now() }, ...events].slice(0, 60);
		};
		es.addEventListener('audit', push('audit'));
		es.addEventListener('erasure', push('erasure'));
	});
	onDestroy(() => { es?.close(); clearInterval(poll); });

	async function fireStorm() {
		const d = await api('/ops/storm', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ n: stormN, read_rps: 40 })
		});
		stormId = d.run_id;
	}

	const stateColors: Record<string, string> = {
		authorized: 'var(--green)', revoked: 'var(--red-soft)', suspended: 'var(--amber)',
		expired: 'var(--ink-faint)', awaiting_authorization: 'var(--blue)'
	};
</script>

<div class="page">
	<div class="topgrid">
		<div class="card stat">
			<h3>Consolidated rows</h3>
			<div class="big money">{fmtRM(metrics?.counts?.transactions ?? 0, 0)}</div>
			<div class="statsub mono">transactions · {fmtRM(metrics?.counts?.profiles ?? 0, 0)} profiles</div>
		</div>
		<div class="card stat">
			<h3>Consent ledger</h3>
			<div class="states">
				{#each Object.entries(metrics?.consent_states ?? {}) as [s, n]}
					<div class="staterow">
						<i style="background: {stateColors[s] ?? '#666'}"></i>
						<span class="mono">{s}</span><b>{n}</b>
					</div>
				{/each}
			</div>
		</div>
		<div class="card stat">
			<h3>Physical erasure (lifetime)</h3>
			<div class="big money red">{fmtRM(deletesTotal, 0)}</div>
			<div class="statsub mono">docs deleted · {metrics?.erasure_jobs?.completed?.count ?? 0} jobs completed</div>
		</div>
		<div class="card stat storm">
			<div class="stormhead">
				<h3>Revocation storm</h3>
				<button class="howbtn" onclick={() => (showHow = true)}>How it works</button>
			</div>
			<div class="stormrow">
				<select bind:value={stormN}>
					<option value={50}>50 consents</option>
					<option value={200}>200 consents</option>
					<option value={500}>500 consents</option>
				</select>
				<button class="btn primary" onclick={fireStorm}>FIRE</button>
			</div>
			{#if stormRun}
				<div class="stormstat mono">
					<div>
						{stormRun.status} · revoked {stormRun.revoked}/{stormRun.found}
						{#if stormRun.errors}· <span class="warn" title={(stormRun.error_samples ?? []).join('\n')}>{stormRun.errors} failed</span>{/if}
					</div>
					{#if stormRun.read_p50_ms}
						<div>read p50 <b>{stormRun.read_p50_ms}ms</b> · p99 <b>{stormRun.read_p99_ms}ms</b> ({stormRun.reads_sampled} samples)</div>
					{/if}
					{#if stormRun.erasure}
						<div>
							erasure: {#each Object.entries(stormRun.erasure) as [s, e]}{@const ej = e as any}{s} {ej.n} ({fmtRM((ej.docs ?? 0) + (ej.snapshots ?? 0), 0)} docs) {/each}
						</div>
					{/if}
				</div>
			{/if}
			<button class="btn relink" onclick={fireReseed} title="Re-link revoked cohort consents through the mock and re-backfill — replenishes data the storm erased">
				↻ Re-link cohort (replenish)
			</button>
			{#if reseedRun}
				<div class="stormstat mono">
					reseed <span class:livedot={reseedRun.status === 'running'}>{reseedRun.status}</span>
					· re-linked {reseedRun.consents_relinked ?? 0}/{reseedRun.found}
					· backfills {reseedRun.backfills_done ?? 0}
					· {fmtRM(reseedRun.transactions ?? 0, 0)} txns
				</div>
			{/if}
		</div>
	</div>

	<div class="grid" style="grid-template-columns: 1.4fr 1fr; margin-top: 18px; align-items: start">
		<div class="card">
			<h3>Live pipeline feed · change streams → SSE</h3>
			<div class="feed">
				{#each events as e (e.at + e.label + (e.doc.consent_id ?? ''))}
					<div class="evt" class:erasure={e.label === 'erasure'}>
						<span class="etime mono">{new Date(e.at).toLocaleTimeString()}</span>
						{#if e.label === 'audit'}
							<span class="chip" style="color: {stateColors[e.doc.status] ?? 'var(--ink-dim)'}">{e.doc.action}</span>
							<span class="edesc mono">
								{e.doc.consent_id} → {e.doc.status}
								{#if e.doc.flip_ms}· flip {e.doc.flip_ms}ms{/if}
								{#if e.doc.docs_deleted !== undefined}· {e.doc.docs_deleted} txns + {e.doc.snapshots_deleted} snapshots erased{/if}
							</span>
						{:else}
							<span class="chip bad">erasure {e.doc.status}</span>
							<span class="edesc mono">
								{e.doc.consent_id} · {e.doc.docs_deleted ?? 0} docs / {e.doc.snapshots_deleted ?? 0} snaps
								· {e.doc.batches ?? 0} batches (max {e.doc.batch_ms_max ?? 0}ms)
							</span>
						{/if}
					</div>
				{/each}
				{#if !events.length}
					<div class="mono dim">waiting for consent events… revoke something under Manage Profile or fire a storm.</div>
				{/if}
			</div>
		</div>

		<div class="card">
			<h3>The claim being proven</h3>
			<ol class="claims">
				<li><b>Gate flip is synchronous + ACID</b> — from the commit, every read path excludes the data, regardless of pending deletes.</li>
				<li><b>Erasure is chunked</b> — 2,000-_id batches off the covering index; no collection scans, no table locks.</li>
				<li><b>Reads don't flinch</b> — One View p99 stays flat during the delete storm (WiredTiger document-level concurrency).</li>
			</ol>
			<div class="path mono">
				Consent Centre click → post-image event (`_rcp_version`) → <b>rcp.consent.events</b> path →
				consents upsert → change stream → gate-flip txn → audit log → erasure job
				<br /><br />
				In production: Acme RCP (MySQL) commit → Debezium outbox → MSK → Kafka Connect → same everything.
			</div>
			<div class="lag mono">audit events last 10 min: {metrics?.audit_events_10m ?? 0}</div>
		</div>
	</div>

	<div class="card" style="margin-top: 18px">
		<h3>Storm run history</h3>
		{#if !history.length}
			<div class="mono dim">No storm runs yet — fire one above.</div>
		{:else}
			<table class="data">
				<thead>
					<tr><th>When</th><th>Status</th><th style="text-align:right">Revoked / found</th><th style="text-align:right">Failed</th><th style="text-align:right">read p50 / p99 / max</th><th style="text-align:right">Samples</th></tr>
				</thead>
				<tbody>
					{#each history as r}
						<tr>
							<td class="mono dim">{new Date(r.started_at).toLocaleString('en-MY', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}</td>
							<td><span class="chip {r.status === 'completed' ? 'ok' : 'warn'}">{r.status}</span></td>
							<td class="mono" style="text-align:right">{r.revoked ?? 0} / {r.found ?? r.requested ?? 0}</td>
							<td class="mono" style="text-align:right" title={(r.error_samples ?? []).join('\n')}>
								<span class:warn={(r.errors ?? 0) > 0}>{r.errors ?? 0}</span>
							</td>
							<td class="mono dim" style="text-align:right">{r.read_p50_ms ?? '—'} / {r.read_p99_ms ?? '—'} / {r.read_max_ms ?? '—'} ms</td>
							<td class="mono dim" style="text-align:right">{r.reads_sampled ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>

	{#if showHow}
		<HowItWorks
			title="Revocation storm — how it works"
			subtitle="N concurrent revocations through the real consent pipeline — the deployed AWS backend runs the Kafka/MSK transport end-to-end — while a steady One View read load measures p50/p99. Expand any step for the API + query."
			footer="Deployed (AWS) runs CONSENT_EVENT_TRANSPORT=kafka: publish → MSK (rcp.consent.events) → MongoDB Kafka Connect sink → consents upsert → change-stream watcher → ACID gate-flip → chunked erasure → self-healing sweeper. The only piece not built is the Acme-side upstream (RCP MySQL → Debezium outbox → MSK), which would emit the byte-identical envelope. The live feed on this page is those change streams over SSE."
			steps={STORM_FLOW}
			onclose={() => (showHow = false)}
		/>
	{/if}
</div>

<style>
	.topgrid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
	.stat .big { font-size: 34px; margin: 2px 0; }
	.big.red { color: var(--red-soft); }
	.statsub { font-size: 10.5px; color: var(--ink-faint); }
	.states { display: flex; flex-direction: column; gap: 5px; }
	.staterow { display: flex; align-items: center; gap: 8px; font-size: 12px; }
	.staterow i { width: 8px; height: 8px; border-radius: 50%; }
	.staterow b { margin-left: auto; }
	.stormhead { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 16px; }
	.stormhead h3 { margin-bottom: 0; }
	.howbtn { background: transparent; border: 1px solid var(--line-2); color: var(--ink-dim);
		font-family: var(--sans); font-size: 10.5px; font-weight: 600; padding: 4px 10px;
		border-radius: 999px; cursor: pointer; transition: all 0.15s; white-space: nowrap; }
	.howbtn:hover { color: var(--ink); border-color: var(--ink-faint); }
	.stormrow { display: flex; gap: 8px; }
	.stormrow select { flex: 1; }
	.storm h3 { color: var(--red-soft); }
	.stormstat { font-size: 11px; color: var(--ink-dim); margin-top: 10px; line-height: 1.7; }

	.feed { max-height: 460px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
	.evt { display: flex; align-items: center; gap: 9px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg-raise); animation: pagein 0.3s; }
	.evt.erasure { border-color: rgba(236, 0, 0, 0.3); }
	.etime { font-size: 10px; color: var(--ink-faint); flex-shrink: 0; }
	.edesc { font-size: 10.5px; color: var(--ink-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.dim { color: var(--ink-faint); font-size: 11.5px; }

	.claims { padding-left: 18px; color: var(--ink-dim); font-size: 13px; line-height: 1.65; }
	.claims li { margin-bottom: 10px; }
	.claims b { color: var(--ink); }
	.path { font-size: 10.5px; color: var(--ink-faint); line-height: 1.7; border-top: 1px solid var(--line); padding-top: 14px; }
	.lag { font-size: 10.5px; color: var(--ink-faint); margin-top: 12px; }
	.warn { color: var(--amber); }
	.relink { width: 100%; margin-top: 10px; font-size: 11px; background: transparent;
		border: 1px solid color-mix(in srgb, var(--green) 35%, var(--line)); color: var(--ink-dim); }
	.relink:hover { color: var(--ink); border-color: color-mix(in srgb, var(--green) 55%, var(--line)); }
	.livedot::before { content: '●'; color: var(--green); margin-right: 4px; animation: pulse 1.3s infinite; }
	@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
</style>
