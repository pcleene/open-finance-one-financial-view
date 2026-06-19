<script lang="ts">
	import { onMount } from 'svelte';
	import { api, cid, fmtRM, CATEGORY_LABELS, CATEGORY_COLORS, SHORT_NAMES } from '$lib/api';
	import CashflowBars from '$lib/CashflowBars.svelte';

	let spend: any = $state(null);
	let cashflow: any = $state(null);
	let networth: any = $state(null);
	let recurring: any = $state(null);
	let sts: any = $state(null);
	let map: any = $state(null);
	let blocked = $state(false);

	onMount(async () => {
		try {
			[spend, cashflow, networth, recurring, sts, map] = await Promise.all([
				api(`/pfm/${cid()}/spend`),
				api(`/pfm/${cid()}/cashflow?months=6`),
				api(`/pfm/${cid()}/net-worth`),
				api(`/pfm/${cid()}/recurring`),
				api(`/pfm/${cid()}/safe-to-spend`),
				api(`/pfm/${cid()}/money-map`)
			]);
		} catch (e: any) {
			if (e.status === 403) blocked = true;
		}
	});

	// ---- donut geometry ----
	const donutSegs = $derived.by(() => {
		if (!spend?.categories?.length) return [];
		const total = spend.categories.reduce((s: number, c: any) => s + parseFloat(c.total), 0);
		let acc = 0;
		return spend.categories.map((c: any) => {
			const frac = parseFloat(c.total) / total;
			const seg = { ...c, frac, start: acc, color: CATEGORY_COLORS[c.category] ?? '#666' };
			acc += frac;
			return seg;
		});
	});
	const TAU = Math.PI * 2;
	function arc(start: number, frac: number, r = 74, w = 26) {
		const a0 = start * TAU - Math.PI / 2, a1 = (start + Math.max(frac - 0.004, 0.001)) * TAU - Math.PI / 2;
		const large = frac > 0.5 ? 1 : 0;
		const x0 = 100 + r * Math.cos(a0), y0 = 100 + r * Math.sin(a0);
		const x1 = 100 + r * Math.cos(a1), y1 = 100 + r * Math.sin(a1);
		return { d: `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`, w };
	}
	const spendTotal = $derived(spend?.categories?.reduce((s: number, c: any) => s + parseFloat(c.total), 0) ?? 0);

	// recurring rows actually rendered (salary + loan repayments excluded — they
	// have their own surfaces). Drives the empty-state placeholder below.
	const recurringGroups = $derived(
		(recurring?.groups ?? [])
			.filter((g: any) => g.category !== 'salary_income' && g.category !== 'loan_repayment')
			.slice(0, 8)
	);

	// ---- net worth path ----
	const nwPath = $derived.by(() => {
		const t = networth?.trend ?? [];
		if (t.length < 2) return { line: '', area: '', min: 0, max: 0, pts: [] };
		const vals = t.map((p: any) => p.net);
		const min = Math.min(...vals), max = Math.max(...vals);
		const W = 560, H = 150, pad = 8;
		const x = (i: number) => pad + (i / (t.length - 1)) * (W - 2 * pad);
		const y = (v: number) => H - pad - ((v - min) / (max - min || 1)) * (H - 2 * pad);
		const pts = t.map((p: any, i: number) => ({ x: x(i), y: y(p.net), v: p.net }));
		const line = pts.map((p: any, i: number) => `${i ? 'L' : 'M'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
		return { line, area: `${line} L ${pts.at(-1).x} ${H} L ${pts[0].x} ${H} Z`, min, max, pts };
	});
</script>

<div class="page">
	{#if blocked}
		<div class="card" style="border-color: rgba(236,0,0,.4)">
			<b style="color:var(--red-soft)">403 Consent.InvalidScope</b> — PFM needs an authorized <code>pfm</code> consent.
		</div>
	{:else if !spend}
		<div class="grid" style="grid-template-columns: 1fr 1fr">
			{#each Array(4) as _}<div class="skeleton" style="height:260px"></div>{/each}
		</div>
	{:else}
		<div class="grid top">
			<div class="card">
				<h3>Spend by category · {spend.month}</h3>
				<div class="donutrow">
					<svg viewBox="0 0 200 200" width="190" height="190">
						{#each donutSegs as s, i}
							{@const a = arc(s.start, s.frac)}
							<path d={a.d} stroke={s.color} stroke-width={a.w} fill="none" stroke-linecap="butt"
								style="animation: segin .7s {i * 60}ms both" />
						{/each}
						<text x="100" y="94" text-anchor="middle" class="donut-total money">RM {fmtRM(spendTotal, 0)}</text>
						<text x="100" y="114" text-anchor="middle" class="donut-sub">this month</text>
					</svg>
					<div class="cats">
						{#each donutSegs.slice(0, 7) as s}
							<div class="catrow">
								<i style="background:{s.color}"></i>
								<span class="catname">{CATEGORY_LABELS[s.category] ?? s.category}</span>
								{#if s.mom_delta_pct !== null && s.mom_delta_pct !== undefined}
									<span class="mom" class:up={s.mom_delta_pct > 0}>{s.mom_delta_pct > 0 ? '▲' : '▼'} {Math.abs(s.mom_delta_pct)}%</span>
								{/if}
								<b class="money">RM {fmtRM(s.total, 0)}</b>
							</div>
						{/each}
					</div>
				</div>
			</div>

			<div class="card">
				<h3>Safe to spend · before {sts?.next_payday}</h3>
				<div class="sts money">RM {fmtRM(sts?.safe_to_spend)}</div>
				<div class="stsrow"><span>Available cash</span><b class="money">RM {fmtRM(sts?.available_cash)}</b></div>
				<div class="stsrow"><span>Committed before payday</span><b class="money">− RM {fmtRM(sts?.committed_before_payday)}</b></div>
				<div class="commits">
					{#each (sts?.commitments ?? []).slice(0, 5) as c}
						<div class="commit">
							<span class="chip">{c.kind.replace('_', ' ')}</span>
							<span class="cname">{c.label}</span>
							<span class="mono cdate">{c.due_date ?? ''}</span>
							<b class="money">RM {fmtRM(c.amount)}</b>
						</div>
					{/each}
				</div>
			</div>
		</div>

		<div class="grid mid">
			<CashflowBars series={cashflow?.series ?? []} months={6} />

			<div class="card">
				<h3>Net worth trend · 13 weeks · from balance_snapshots</h3>
				<svg viewBox="0 0 560 150" width="100%" height="150" preserveAspectRatio="none">
					<defs>
						<linearGradient id="nw" x1="0" y1="0" x2="0" y2="1">
							<stop offset="0" stop-color="rgba(62,207,142,.35)" />
							<stop offset="1" stop-color="rgba(62,207,142,0)" />
						</linearGradient>
					</defs>
					<path d={nwPath.area} fill="url(#nw)" />
					<path d={nwPath.line} fill="none" stroke="var(--green)" stroke-width="2.5"
						style="stroke-dasharray: 1200; stroke-dashoffset: 1200; animation: draw 1.4s .2s forwards" />
				</svg>
				<div class="nwlabels mono">
					<span>now: RM {fmtRM(networth?.now?.amount)}</span>
					<span>range: {fmtRM(nwPath.min, 0)} → {fmtRM(nwPath.max, 0)}</span>
				</div>
			</div>
		</div>

		<div class="grid mid">
			<div class="card">
				<h3>Recurring & subscriptions</h3>
				{#if recurringGroups.length}
					{#each recurringGroups as g}
						<div class="rec">
							<i style="background:{CATEGORY_COLORS[g.category] ?? '#666'}"></i>
							<div class="recmain">
								<div>{g.label}
									{#if g.zombie}<span class="chip bad" title="no activity for 60+ days">zombie</span>{/if}
									{#if g.duplicate_candidate}<span class="chip warn">duplicate?</span>{/if}
								</div>
								<div class="recsub mono">{g.period} · next ≈ {g.next_expected} · {SHORT_NAMES[g.institution] ?? g.institution}</div>
							</div>
							<b class="money">RM {fmtRM(g.amount)}</b>
						</div>
					{/each}
				{:else}
					<div class="rec-empty">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
							<path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99" />
						</svg>
						<p>No recurring transactions detected</p>
						<small>Subscriptions and regular payments will appear here once a repeating pattern is found.</small>
					</div>
				{/if}
			</div>

			<div class="card">
				<h3>Money map · {spend.month}</h3>
				<div class="map">
					<div class="mapcol">
						<div class="maphead">IN</div>
						{#each (map?.income ?? []).slice(0, 5) as f}
							<div class="flow in">
								<span>{CATEGORY_LABELS[f._id.cat] ?? f._id.cat}<small>{f._id.inst}</small></span>
								<b class="money">{fmtRM(f.total, 0)}</b>
							</div>
						{/each}
						{#if !(map?.income ?? []).length}<div class="mono dim2">no income rows yet this month</div>{/if}
					</div>
					<div class="mapmid">→</div>
					<div class="mapcol">
						<div class="maphead">OUT</div>
						{#each (map?.spending ?? []).toSorted((a: any, b: any) => b.total - a.total).slice(0, 7) as f}
							<div class="flow out">
								<span>{CATEGORY_LABELS[f._id.cat] ?? f._id.cat}<small>{f._id.inst}</small></span>
								<b class="money">{fmtRM(f.total, 0)}</b>
							</div>
						{/each}
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.top { grid-template-columns: 1.25fr 1fr; }
	.mid { grid-template-columns: 1fr 1fr; margin-top: 18px; }

	.donutrow { display: flex; gap: 22px; align-items: center; }
	:global(svg path) { transform-origin: center; }
	@keyframes -global-segin { from { opacity: 0; } to { opacity: 1; } }
	@keyframes -global-draw { to { stroke-dashoffset: 0; } }
	.donut-total { fill: var(--ink); font-family: var(--serif); font-size: 21px; }
	.donut-sub { fill: var(--ink-faint); font-size: 10px; font-family: var(--mono); }
	.cats { flex: 1; }
	.catrow { display: flex; align-items: center; gap: 9px; padding: 5.5px 0; font-size: 13px; }
	.catrow i { width: 9px; height: 9px; border-radius: 3px; flex-shrink: 0; }
	.catname { flex: 1; color: var(--ink-dim); }
	.mom { font-size: 10.5px; font-family: var(--mono); color: var(--green); }
	.mom.up { color: var(--red-soft); }

	.sts { font-size: 42px; margin: 4px 0 14px; }
	.stsrow { display: flex; justify-content: space-between; padding: 7px 0; border-top: 1px solid var(--line); font-size: 13.5px; color: var(--ink-dim); }
	.commits { margin-top: 10px; }
	.commit { display: flex; align-items: center; gap: 9px; font-size: 12.5px; padding: 6px 0; border-top: 1px solid var(--line); }
	.cname { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.cdate { color: var(--ink-faint); font-size: 10.5px; }

	.nwlabels { display: flex; justify-content: space-between; font-size: 11px; color: var(--ink-dim); margin-top: 8px; }

	.rec { display: flex; align-items: center; gap: 11px; padding: 8px 0; border-top: 1px solid var(--line); }
	.rec:first-of-type { border-top: 0; }
	.rec i { width: 9px; height: 9px; border-radius: 3px; flex-shrink: 0; }
	.recmain { flex: 1; font-size: 13.5px; }
	.recsub { font-size: 10.5px; color: var(--ink-faint); margin-top: 2px; }

	.rec-empty { display: flex; flex-direction: column; align-items: center; justify-content: center;
		text-align: center; padding: 30px 16px; gap: 4px; min-height: 150px; }
	.rec-empty svg { width: 30px; height: 30px; color: var(--ink-faint); opacity: 0.6; margin-bottom: 6px; }
	.rec-empty p { margin: 0; font-size: 13.5px; color: var(--ink-dim); }
	.rec-empty small { font-size: 11px; color: var(--ink-faint); max-width: 280px; line-height: 1.5; }

	.map { display: flex; gap: 14px; align-items: flex-start; }
	.mapcol { flex: 1; }
	.maphead { font-size: 10px; letter-spacing: 0.18em; color: var(--ink-faint); margin-bottom: 8px; }
	.mapmid { color: var(--ink-faint); font-size: 22px; align-self: center; }
	.flow { display: flex; justify-content: space-between; align-items: center; font-size: 12.5px; padding: 7px 10px; border-radius: 8px; margin-bottom: 6px; }
	.flow small { display: block; color: var(--ink-faint); font-size: 10px; }
	.flow.in { background: rgba(62, 207, 142, 0.07); border: 1px solid rgba(62, 207, 142, 0.18); }
	.flow.out { background: rgba(236, 0, 0, 0.06); border: 1px solid rgba(236, 0, 0, 0.16); }
	.dim2 { color: var(--ink-faint); font-size: 11px; }
</style>
