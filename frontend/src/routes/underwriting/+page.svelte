<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, cid, fmtRM, fmtDateTime, SHORT_NAMES } from '$lib/api';
	import { fmt, toShell, highlight } from '$lib/queryFmt';
	import HowItWorks from '$lib/HowItWorks.svelte';

	let showHow = $state(false);

	let run: any = $state(null);
	let runs: any[] = $state([]);
	let features: any = $state(null);
	let blocked: any = $state(null);
	let running = $state(false);
	let showSnapshot = $state(false);
	let gauge = $state(0);

	// staged progress (the run is one POST; we light the known pipeline steps
	// while it is in flight, then reveal the real per-step timings on completion)
	const STEPS = [
		{ key: 'consent_gate', label: 'Consent gate' },
		{ key: 'build_feature_store', label: 'Build feature store' },
		{ key: 'salary_statistics', label: 'Salary statistics' },
		{ key: 'score_in_aggregation', label: 'Score inside aggregation' },
		{ key: 'persist_run', label: 'Persist immutable run' }
	];
	let activeStep = $state(-1);
	let stepTimer: ReturnType<typeof setInterval>;
	let opsRun: any = $state(null); // run whose operations modal is open
	let openOps = $state<Record<number, boolean>>({}); // expanded operation rows in the popup

	function showOps(r: any) {
		opsRun = r;
		openOps = {}; // collapsed by default each time the popup opens
	}
	function toggleOp(idx: number) {
		openOps = { ...openOps, [idx]: !openOps[idx] };
	}

	function startStaged() {
		activeStep = 0;
		clearInterval(stepTimer);
		stepTimer = setInterval(() => {
			if (activeStep < STEPS.length - 1) activeStep += 1; // hold last until response
		}, 420);
	}
	function stopStaged() {
		clearInterval(stepTimer);
		activeStep = STEPS.length;
	}
	onDestroy(() => clearInterval(stepTimer));

	const opTotal = $derived(opsRun?.latency_ms?.total || 1);
	function opPct(ms: number) {
		return Math.max(2, Math.min(100, Math.round((ms / opTotal) * 100)));
	}

	// "How it works" — the real data & operations behind a loan inquiry, end-to-end.
	const UW_FLOW = [
		{
			title: 'Consent gate — resolve scope',
			tone: 'mongo',
			api: { method: 'POST', path: '/api/underwriting/{customer_id}/run' },
			file: 'consent/gate.py · resolve_consent_scope()',
			desc: 'Underwriting reads NOTHING until the gate resolves which accounts the customer has authorized for credit_underwriting — right now. The live consent state is the boxes embedded on each account, so enforcement is one indexed profile read, no joins.',
			code: `db.customer_profiles.find_one(
  { _id: customer_id },
  { "accounts.account_id": 1, "accounts.is_internal": 1, "accounts.consents": 1 }
)
// keep an account only if a box is, right now:
//   status == "authorized"
//   consent_purpose ∈ credit_underwriting purposes
//   "read_transactions" ∈ permissions
//   expiration_datetime > now           // EOD expiry self-enforces
// scope == ∅  →  HTTP 403 Consent.InvalidScope  (refuse — never a thin score)`,
			note: 'Same data, same model — if the customer only granted PFM consent the scope resolves to ∅ and the inquiry is refused at the read path.'
		},
		{
			title: 'Build the feature store — on demand',
			tone: 'mongo',
			file: 'ingestion/features.py · rebuild_uw_features()',
			desc: 'Reactive scoring (brief §10): the per-account feature components are rolled up and persisted exactly when a loan inquiry runs — not nightly for every customer. The rollup spans only the consented account set.',
			code: `// 6-month per-account monthly components, scoped to the consented accounts
db.transactions.aggregate([
  { $match: { customer_id, "account.account_id": { $in: scope },
              "enrichment.is_transfer_own_account": false } },
  { $group: {
      _id: { acct: "$account.account_id", month: "$enrichment.month" },
      inflow:  { $sum: { $cond: [{ $eq: ["$credit_debit_indicator", "credit"] }, "$amount.amount", 0] } },
      outflow: { $sum: { $cond: [{ $eq: ["$credit_debit_indicator", "debit"]  }, "$amount.amount", 0] } },
      gambling_spend: { $sum: { $cond: [{ $eq: ["$enrichment.category", "gambling"] }, "$amount.amount", 0] } } } }
])
// + balance_stats_90d (avg/min EOD, days_below_500) from the reconstructed
//   balance_snapshots, then persist the decomposable feature document:
db.uw_features.replace_one({ _id: customer_id }, featureDoc, { upsert: true })`
		},
		{
			title: 'Salary statistics',
			tone: 'mongo',
			file: 'services/underwriting_service.py · _salary_stats()',
			desc: 'Median monthly salary, income regularity and payday — inferred from the consented salary_income credits over the last ~6 months, entirely in an aggregation.',
			code: `db.transactions.aggregate([
  { $match: { customer_id, "account.account_id": { $in: scope },
              "enrichment.category": "salary_income",
              credit_debit_indicator: "credit",
              transaction_date: { $gte: now_minus_185d } } },
  { $group: { _id: "$enrichment.month",
              total: { $sum: { $toDouble: "$amount.amount" } },
              day:   { $max: { $dayOfMonth: "$transaction_date" } } } },
  { $group: { _id: null,
              median_salary: { $median: { input: "$total", method: "approximate" } },
              mean:   { $avg: "$total" },
              stddev: { $stdDevPop: "$total" },
              months_observed: { $sum: 1 },
              payday: { $median: { input: "$day", method: "approximate" } } } }
])
// regularity = max(0, 1 − stddev / mean)   → income-stability signal`
		},
		{
			title: 'Score inside the aggregation',
			tone: 'mongo',
			file: 'services/underwriting_service.py · score_pipeline()',
			desc: 'The whole scorecard — rollups, weighted score, band and decision — is computed in ONE $facet pipeline over uw_features. The data never leaves the database to be scored (computed_in_db: true).',
			code: `db.uw_features.aggregate([
  { $match: { _id: customer_id } },
  // consent scope enforced again, in-DB:
  { $project: { scoped: { $filter: { input: "$accounts", as: "a",
                          cond: { $in: ["$$a.account_id", scope] } } } } },
  { $facet: { flows: [ /*…*/ ], credit: [ /*…*/ ], loans: [ /*…*/ ], liquidity: [ /*…*/ ] } },
  { $addFields: {
      "rollup.utilization_now": { $divide: ["$rollup.portfolio_owed", "$rollup.portfolio_limit"] },
      "rollup.dsr_estimate":    { $divide: [{ $add: ["$rollup.loan_installments", "$rollup.card_min_payments"] }, salary] } } },
  { $addFields: { score: { $round: [{ $add: [
        600,
        { $multiply: [ 85, regularity] },
        { $multiply: [ 70, { $min: [{ $divide: ["$rollup.avg_eod", 5000] }, 1] }] },
        { $multiply: [-85, "$rollup.utilization_now"] },
        { $multiply: [-110, { $min: ["$rollup.dsr_estimate", 1] }] },
        { $multiply: [-90, { $min: [{ $divide: ["$rollup.gambling_6m", 1000] }, 1] }] }
  ] }, 0] } } },
  { $addFields: {
      band:     { $switch: { branches: [ { case: { $gte: ["$score", 740] }, then: "A" } ], default: "D" } },
      decision: { $switch: { branches: [
        { case: { $gte: ["$score", 700] }, then: "approve" },
        { case: { $gte: ["$score", 640] }, then: "approve_with_conditions" } ], default: "decline" } } } }
])`
		},
		{
			title: 'Persist an immutable, consent-stamped run',
			tone: 'mongo',
			file: 'services/underwriting_service.py · run()',
			desc: 'The decision is appended to underwriting_runs with verbatim copies of the governing consents and the exact feature components used. Re-running with these snapshots reproduces the identical score — the BNM audit story.',
			code: `db.underwriting_runs.insert_one({
  customer_id, run_at: now, requested_product,
  consent_snapshot: [ /* verbatim governing credit_underwriting consents */ ],
  scope_account_ids: scope,
  features_snapshot: { components, rollup, derived },
  scorecard: { model_version, score, band, decision, reason_codes, computed_in_db: true },
  latency_ms: { gate, features_build, features, score, persist, total },
  operations: [ /* the real per-step MongoDB ops, captured by the Query Inspector */ ]
})
// append-only — a customer who revokes can't produce a NEW run, but this one stands.`
		}
	];

	onMount(async () => {
		const h = await api(`/underwriting/${cid()}/runs`);
		runs = h.runs;
		features = await api(`/underwriting/${cid()}/features`);
		if (runs.length) { run = runs[0]; animateGauge(run.scorecard.score); }
	});

	function animateGauge(target: number) {
		const t0 = performance.now();
		const tick = (t: number) => {
			const k = Math.min(1, (t - t0) / 900);
			gauge = target * (1 - Math.pow(1 - k, 3));
			if (k < 1) requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	}

	async function executeRun() {
		running = true;
		blocked = null;
		run = null;
		startStaged();
		try {
			const r = await api(`/underwriting/${cid()}/run`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ product: 'personal_loan_50k_60m' })
			});
			stopStaged();
			run = r;
			animateGauge(run.scorecard.score);
			const h = await api(`/underwriting/${cid()}/runs`);
			runs = h.runs;
		} catch (e: any) {
			if (e.status === 403) blocked = e.body?.detail ?? { error: 'Consent.InvalidScope' };
		} finally {
			clearInterval(stepTimer);
		}
		running = false;
	}

	const decisionColor: Record<string, string> = {
		approve: 'var(--green)', approve_with_conditions: 'var(--amber)',
		manual_review: 'var(--blue)', decline: 'var(--red-soft)'
	};
	// gauge geometry: 300..850 mapped over a 240° arc
	const frac = $derived(Math.max(0, Math.min(1, (gauge - 300) / 550)));
	function polar(angleDeg: number, r: number) {
		const a = ((angleDeg - 90) * Math.PI) / 180;
		return [110 + r * Math.cos(a), 110 + r * Math.sin(a)];
	}
	const gaugePath = $derived.by(() => {
		const start = -120, end = -120 + 240 * frac;
		const [x0, y0] = polar(start, 86);
		const [x1, y1] = polar(end, 86);
		return `M ${x0} ${y0} A 86 86 0 ${end - start > 180 ? 1 : 0} 1 ${x1} ${y1}`;
	});
	const trackPath = (() => {
		const [x0, y0] = polar(-120, 86);
		const [x1, y1] = polar(120, 86);
		return `M ${x0} ${y0} A 86 86 0 1 1 ${x1} ${y1}`;
	})();
</script>

<div class="page">
	<div class="head">
		<div>
			<h2 style="font-family: var(--serif); font-size: 26px; font-weight: 500">Credit Underwriting Console</h2>
			<p class="sub">Scorecard computed <b>inside an aggregation pipeline</b> over the consented account set only — banker persona view.</p>
		</div>
		<div class="headbtns">
			<button class="btn" onclick={() => (showHow = true)}>How it works</button>
			<button class="btn primary" disabled={running} onclick={executeRun}>
				{running ? 'Scoring…' : 'Run scorecard · personal loan RM50k/60m'}
			</button>
		</div>
	</div>

	{#if blocked}
		<div class="card blockedcard">
			<div class="code mono">HTTP 403</div>
			<div class="errname mono">{blocked.error}</div>
			<p>{blocked.detail}</p>
			<p class="why">
				This customer has <code>pfm</code> consent only. The gate resolved the
				<code>credit_underwriting</code> scope to ∅ — same data, same feature store,
				<b>refused at the read path</b>. No thin score, no silent fallback.
			</p>
		</div>
	{/if}

	{#if running}
		<div class="card progresscard">
			<div class="phead"><span class="spin"></span> Running credit underwriting — reactive feature build + score…</div>
			<div class="pbar"><div class="pfill" style="width:{Math.round(((activeStep + 1) / STEPS.length) * 100)}%"></div></div>
			<ol class="psteps">
				{#each STEPS as s, idx}
					<li class:done={idx < activeStep} class:active={idx === activeStep}>
						<span class="dot"></span>{s.label}
					</li>
				{/each}
			</ol>
		</div>
	{/if}

	{#if run && !blocked}
		<div class="grid" style="grid-template-columns: 360px 1fr; align-items: start">
			<div class="card scorecard">
				<h3>Decision · {run.scorecard.model_version}</h3>
				<svg viewBox="0 0 220 200" width="100%">
					<path d={trackPath} fill="none" stroke="var(--line-2)" stroke-width="13" stroke-linecap="round" />
					<path d={gaugePath} fill="none" stroke={decisionColor[run.scorecard.decision]} stroke-width="13" stroke-linecap="round" />
					<text x="110" y="104" text-anchor="middle" class="score money">{Math.round(gauge)}</text>
					<text x="110" y="128" text-anchor="middle" class="band">band {run.scorecard.band}</text>
				</svg>
				<div class="decision" style="color: {decisionColor[run.scorecard.decision]}">
					{run.scorecard.decision.replaceAll('_', ' ').toUpperCase()}
				</div>
				<div class="reasons">
					{#each run.scorecard.reason_codes as rc}<span class="chip">{rc}</span>{/each}
				</div>
				<div class="lat mono">
					gate {run.latency_ms.gate}ms · build {run.latency_ms.features_build ?? '—'}ms ·
					salary {run.latency_ms.features}ms · score-in-DB {run.latency_ms.score}ms ·
					total {run.latency_ms.total}ms
				</div>
				{#if run.operations?.length}
					<button class="btn opsbtn" onclick={() => showOps(run)}>⟷ operations performed</button>
				{/if}
			</div>

			<div>
				<div class="card">
					<h3>Feature rollup — derived only from consented accounts</h3>
					<div class="feats">
						{#each [
							['Median monthly salary', `RM ${fmtRM(run.features_snapshot.rollup.median_monthly_salary)}`],
							['Salary regularity', run.features_snapshot.rollup.salary_regularity_score],
							['DSR estimate', run.features_snapshot.rollup.dsr_estimate],
							['Portfolio utilization', run.features_snapshot.rollup.utilization_now],
							['Avg EOD balance 90d', `RM ${fmtRM(run.features_snapshot.rollup.avg_eod)}`],
							['Min EOD balance 90d', `RM ${fmtRM(run.features_snapshot.rollup.min_eod)}`],
							['Days below RM500', run.features_snapshot.rollup.days_below_500],
							['Gambling spend 6m', `RM ${fmtRM(run.features_snapshot.rollup.gambling_6m)}`],
							['Net flow 6m', `RM ${fmtRM(run.features_snapshot.rollup.net_flow)}`],
							['Loan installments / m', `RM ${fmtRM(run.features_snapshot.rollup.loan_installments)}`]
						] as [k, v]}
							<div class="feat"><span>{k}</span><b class="money">{v}</b></div>
						{/each}
					</div>
				</div>

				<div class="card" style="margin-top: 16px">
					<h3>
						Consent-stamped & reproducible
						<button class="btn" style="float: right; padding: 4px 12px; font-size: 11px"
							onclick={() => (showSnapshot = !showSnapshot)}>
							{showSnapshot ? 'hide' : 'view'} snapshot
						</button>
					</h3>
					<p class="sub" style="margin: 0">
						This run embeds verbatim copies of the {run.consent_snapshot.length} governing
						credit_underwriting consent(s) and the exact feature components used —
						{run.scope_account_ids.length} accounts in scope. Re-running with these snapshots
						reproduces the identical score; a customer who revokes can no longer produce a
						<i>new</i> run, but this document stands. That is the BNM audit story.
					</p>
					{#if showSnapshot}
						<pre class="mono snap">{JSON.stringify({ consent_snapshot: run.consent_snapshot, scope_account_ids: run.scope_account_ids }, null, 2)}</pre>
					{/if}
				</div>
			</div>
		</div>
	{/if}

	<div class="card" style="margin-top: 16px">
		<h3>Run history (immutable, append-only)</h3>
		<table class="data">
			<thead><tr><th>When</th><th>Product</th><th>Score</th><th>Band</th><th>Decision</th><th>Scope</th><th>Total ms</th><th></th></tr></thead>
			<tbody>
				{#each runs as r}
					<tr>
						<td class="mono">{fmtDateTime(r.run_at)}</td>
						<td class="mono">{r.requested_product}</td>
						<td class="money" style="font-size: 16px">{r.scorecard.score}</td>
						<td>{r.scorecard.band}</td>
						<td style="color: {decisionColor[r.scorecard.decision]}">{r.scorecard.decision.replaceAll('_', ' ')}</td>
						<td class="mono">{r.scope_account_ids.length} accounts</td>
						<td class="mono">{r.latency_ms.total}</td>
						<td style="text-align: right">
							{#if r.operations?.length}
								<button class="btn ghost opscell" onclick={() => showOps(r)}>⟷</button>
							{/if}
						</td>
					</tr>
				{/each}
				{#if !runs.length}<tr><td colspan="8" class="mono" style="color: var(--ink-faint)">no runs yet</td></tr>{/if}
			</tbody>
		</table>
	</div>

	{#if features}
		<div class="card" style="margin-top: 16px">
			<h3>Feature store coverage — “explain my data”</h3>
			<div class="cov">
				{#each features._consent_coverage as c}
					<div class="covrow">
						<span class="mono covacct">{SHORT_NAMES[c.dp_id] ?? c.dp_id} · {c.type}/{c.subtype}</span>
						<span class="chip {c.in_pfm_scope ? 'ok' : ''}">pfm {c.in_pfm_scope ? '✓' : '✗'}</span>
						<span class="chip {c.in_credit_underwriting_scope ? 'ok' : 'bad'}">underwriting {c.in_credit_underwriting_scope ? '✓' : '✗'}</span>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	{#if showHow}
		<HowItWorks
			title="Credit underwriting — how it works"
			subtitle="One loan inquiry, end-to-end: consent-gated reads, an on-demand feature build, and a scorecard computed entirely inside MongoDB. Expand any step for the API + query."
			footer="Reactive scoring (brief §10): the feature store is built on demand at inquiry and the scorecard is computed in-aggregation. The real per-step timings + pipelines for each run are captured live by the Query Inspector — see ⟷ operations performed."
			steps={UW_FLOW}
			onclose={() => (showHow = false)}
		/>
	{/if}

	{#if opsRun}
		<div
			class="ops-backdrop"
			role="button"
			tabindex="0"
			onclick={() => (opsRun = null)}
			onkeydown={(e) => e.key === 'Escape' && (opsRun = null)}
		>
			<div class="ops-modal" role="dialog" tabindex="0" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
				<div class="ops-head">
					<div>
						<h3 style="margin: 0">Operations performed</h3>
						<div class="mono ops-sub">
							{fmtDateTime(opsRun.run_at)} · total {opsRun.latency_ms?.total}ms ·
							{opsRun.operations.reduce((n: number, o: any) => n + (o.query_count ?? 0), 0)} MongoDB ops
						</div>
					</div>
					<button class="btn" onclick={() => (opsRun = null)}>close</button>
				</div>
				<ol class="ops-list">
					{#each opsRun.operations as o, idx}
						<li class="ops-item" class:open={openOps[idx]}>
							<button class="ops-top" onclick={() => toggleOp(idx)} aria-expanded={openOps[idx] ? 'true' : 'false'}>
								<span class="ops-chev" class:rot={openOps[idx]}>›</span>
								<span class="ops-idx mono">{idx + 1}</span>
								<span class="ops-label">{o.label}</span>
								<span class="ops-ms mono">{o.ms}ms</span>
							</button>
							<div class="ops-segbar"><div class="ops-segfill" style="width:{opPct(o.ms)}%"></div></div>
							<p class="ops-detail">{o.detail}</p>
							<div class="ops-meta">
								{#each o.collections as col}<span class="chip mono">{col}</span>{/each}
								<span class="ops-counts mono">
									{o.query_count} {o.query_count === 1 ? 'query' : 'queries'}
									{#if o.result_docs}· {o.result_docs} docs{/if}
								</span>
							</div>

							{#if openOps[idx]}
								<div class="ops-drill">
									{#each (o.queries ?? []) as q}
										<div class="ops-q">
											<div class="ops-q-head mono">
												<span class="ops-qop ops-qop-{q.operation}">{q.operation}</span>
												<span class="ops-qcoll">{q.collection}</span>
												{#if q.result_count != null}
													<span class="ops-qarrow">→</span>
													<span class="ops-qsum">{q.result_count} doc{q.result_count === 1 ? '' : 's'}</span>
												{/if}
												<span class="ops-qms">{Math.round(q.duration_ms ?? 0)} ms</span>
											</div>
											<div class="ops-qsub mono">query</div>
											<pre class="ops-code">{@html highlight(toShell(q))}</pre>
											{#if q.result != null}
												<div class="ops-qsub mono">sample document</div>
												<pre class="ops-code">{@html highlight(fmt(q.result))}</pre>
											{/if}
										</div>
									{/each}
									{#if !(o.queries?.length)}
										<div class="ops-noq mono">No aggregation pipelines captured — this step uses a point lookup / single write (the inspector traces aggregations).</div>
									{/if}
								</div>
							{/if}
						</li>
					{/each}
				</ol>
				<p class="ops-foot mono">
					Reactive scoring (brief §10): the feature store is built on demand at inquiry and
					the scorecard is computed inside the aggregation. Timings + ops captured live by the
					Query Inspector.
				</p>
			</div>
		</div>
	{/if}
</div>

<style>
	.head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 22px; gap: 20px; }
	.headbtns { display: flex; gap: 10px; align-items: center; flex-shrink: 0; }
	.sub { color: var(--ink-dim); font-size: 13px; margin: 6px 0 0; line-height: 1.55; }

	.blockedcard { border-color: rgba(236, 0, 0, 0.45); margin-bottom: 18px; animation: pulse-red 2s infinite; }
	.code { font-size: 11px; color: var(--ink-faint); }
	.errname { font-size: 22px; color: var(--red-soft); margin: 4px 0; }
	.why { color: var(--ink-dim); font-size: 13px; line-height: 1.6; }

	.scorecard { text-align: center; }
	.score { fill: var(--ink); font-family: var(--serif); font-size: 46px; }
	.band { fill: var(--ink-dim); font-size: 12px; font-family: var(--mono); }
	.decision { font-weight: 700; letter-spacing: 0.12em; font-size: 15px; margin-top: -16px; }
	.reasons { display: flex; gap: 6px; justify-content: center; flex-wrap: wrap; margin-top: 14px; }
	.lat { font-size: 10.5px; color: var(--ink-faint); margin-top: 14px; line-height: 1.6; }

	.feats { display: grid; grid-template-columns: 1fr 1fr; gap: 0 26px; }
	.feat { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
	.feat span { color: var(--ink-dim); }

	.snap { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 14px; font-size: 10.5px; max-height: 320px; overflow: auto; margin-top: 12px; }

	.cov { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; }
	.covrow { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--line); }
	.covacct { flex: 1; font-size: 11.5px; color: var(--ink-dim); }

	/* staged progress while the run is in flight */
	.progresscard { margin-bottom: 18px; }
	.phead { font-size: 13px; color: var(--ink); display: flex; align-items: center; gap: 9px; margin-bottom: 12px; }
	.spin { width: 13px; height: 13px; border-radius: 50%; border: 2px solid var(--line-2); border-top-color: var(--blue); display: inline-block; animation: spin 0.8s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	.pbar { height: 8px; border-radius: 5px; background: var(--line); overflow: hidden; }
	.pfill { height: 100%; background: linear-gradient(90deg, var(--blue), color-mix(in srgb, var(--blue) 55%, transparent)); transition: width 0.4s ease; }
	.psteps { list-style: none; margin: 14px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px 20px; }
	.psteps li { display: flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--ink-faint); }
	.psteps li .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--line-2); }
	.psteps li.active { color: var(--ink); }
	.psteps li.active .dot { background: var(--blue); box-shadow: 0 0 0 4px color-mix(in srgb, var(--blue) 22%, transparent); }
	.psteps li.done { color: var(--ink-dim); }
	.psteps li.done .dot { background: var(--green); }

	.opsbtn { margin-top: 12px; font-size: 11px; padding: 5px 12px; }
	.opscell { padding: 2px 10px; font-size: 13px; line-height: 1; }

	/* operations popup */
	.ops-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.55); backdrop-filter: blur(2px); display: flex; align-items: center; justify-content: center; z-index: 60; padding: 24px; }
	.ops-modal { background: var(--card, var(--bg)); border: 1px solid var(--line-2); border-radius: 16px; width: min(640px, 100%); max-height: 86vh; overflow: auto; padding: 22px 24px; box-shadow: 0 24px 60px rgba(0,0,0,0.45); }
	.ops-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
	.ops-sub { font-size: 10.5px; color: var(--ink-faint); margin-top: 5px; }
	.ops-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
	.ops-item { border: 1px solid var(--line); border-radius: 11px; padding: 12px 14px; }
	.ops-item.open { border-color: color-mix(in srgb, var(--blue) 40%, var(--line)); }
	.ops-top { display: flex; align-items: center; gap: 10px; width: 100%; background: transparent; border: 0; padding: 0; cursor: pointer; text-align: left; }
	.ops-chev { color: var(--ink-faint); font-size: 15px; line-height: 1; transition: transform 0.15s; flex: 0 0 auto; }
	.ops-chev.rot { transform: rotate(90deg); color: var(--blue); }
	.ops-idx { width: 20px; height: 20px; border-radius: 50%; background: var(--line); color: var(--ink-dim); font-size: 10px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
	.ops-label { flex: 1; font-size: 13px; font-weight: 600; }
	.ops-ms { font-size: 11px; color: var(--blue); }
	.ops-segbar { height: 5px; border-radius: 4px; background: var(--line); overflow: hidden; margin: 9px 0 8px; }
	.ops-segfill { height: 100%; background: var(--blue); }
	.ops-detail { font-size: 11.5px; color: var(--ink-dim); line-height: 1.55; margin: 0 0 9px; }
	.ops-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
	.ops-meta .chip { font-size: 10px; }
	.ops-counts { font-size: 10px; color: var(--ink-faint); margin-left: auto; }
	.ops-foot { font-size: 10px; color: var(--ink-faint); line-height: 1.6; margin: 16px 0 0; }

	/* per-operation query/result drill-down */
	.ops-drill { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px; display: flex; flex-direction: column; gap: 12px; }
	.ops-q { border: 1px solid var(--line); border-radius: 9px; overflow: hidden; background: rgba(255, 255, 255, 0.015); }
	.ops-q-head { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--line); font-size: 11px; }
	.ops-qop { font-size: 9.5px; padding: 2px 6px; border-radius: 5px; background: rgba(139, 92, 246, 0.16); color: #c4b5fd; }
	.ops-qop-find, .ops-qop-find_one { background: rgba(6, 182, 212, 0.16); color: #67e8f9; }
	.ops-qcoll { color: var(--ink-dim); }
	.ops-qarrow { color: var(--ink-faint); }
	.ops-qsum { color: var(--green); background: rgba(62, 207, 142, 0.1); padding: 1px 6px; border-radius: 5px; font-size: 9.5px; }
	.ops-qms { margin-left: auto; color: var(--blue); font-size: 10px; }
	.ops-qsub { padding: 5px 10px 0; font-size: 8.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.09em; color: var(--ink-faint); }
	.ops-code { margin: 0; padding: 8px 11px 11px; font-size: 10.5px; line-height: 1.55; font-family: var(--mono); color: var(--ink-dim); white-space: pre; overflow: auto; max-height: 280px; }
	.ops-noq { font-size: 10.5px; color: var(--ink-faint); }

	:global(.ops-code .tk-op) { color: #c4b5fd; font-weight: 600; }
	:global(.ops-code .tk-key) { color: #7dd3fc; }
	:global(.ops-code .tk-str) { color: #86efac; }
	:global(.ops-code .tk-num) { color: #fdba74; }
	:global(.ops-code .tk-bool) { color: #fca5a5; }
	:global(.ops-code .tk-dim) { color: #fbbf24; font-style: italic; }
	:global(.ops-code .tk-db) { color: #e2b74b; font-weight: 700; }
	:global(.ops-code .tk-comment) { color: var(--ink-faint); font-style: italic; }
</style>
