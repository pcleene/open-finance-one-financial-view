<script lang="ts">
	import { onMount } from 'svelte';
	import { api, cid, fmtRM, fmtDateTime, INSTITUTION_COLORS, SHORT_NAMES, CATEGORY_LABELS, CATEGORY_COLORS } from '$lib/api';
	import CashflowBars from '$lib/CashflowBars.svelte';

	let rows: any[] = $state([]);
	let cursor: string | null = $state(null);
	let loading = $state(false);
	let done = $state(false);
	let latency = $state(0);
	let blocked = $state(false);
	let cashflow: any = $state(null);

	let category = $state('');
	let institution = $state('');
	let indicator = $state('');

	let query = $state('');
	let mode = $state('hybrid');
	let searching = $state(false);
	let searchMethod = $state('');
	let searchNote: string | null = $state(null);
	let searchTimer: any;
	const searchActive = $derived(query.trim().length > 0);

	async function runSearch() {
		searching = true;
		const p = new URLSearchParams({ q: query.trim(), mode, page_size: '50' });
		try {
			const d = await api(`/pfm/${cid()}/transactions/search?${p}`);
			rows = d.data;
			searchMethod = d.search_method;
			searchNote = d.note;
			done = true;
			blocked = false;
		} catch (e: any) {
			if (e.status === 403) blocked = true;
			rows = [];
		}
		searching = false;
	}

	function onSearchInput() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			if (searchActive) runSearch();
			else { searchMethod = ''; searchNote = null; load(true); }
		}, 300);
	}

	function setMode(m: string) {
		mode = m;
		if (searchActive) runSearch();
	}

	function clearSearch() {
		query = '';
		searchMethod = '';
		searchNote = null;
		load(true);
	}

	async function loadCashflow() {
		const p = new URLSearchParams({ months: '6' });
		if (institution) p.set('institution', institution);
		if (indicator) p.set('indicator', indicator);
		try {
			cashflow = await api(`/pfm/${cid()}/cashflow?${p}`);
		} catch {
			cashflow = null;
		}
	}

	async function load(reset = false) {
		if (searchActive && !reset) return; // search owns the list while active
		if (loading || (done && !reset)) return;
		loading = true;
		if (reset) { rows = []; cursor = null; done = false; blocked = false; }
		const p = new URLSearchParams({ page_size: '50' });
		if (cursor) p.set('cursor', cursor);
		if (category) p.set('category', category);
		if (institution) p.set('institution', institution);
		if (indicator) p.set('indicator', indicator);
		try {
			const d = await api(`/pfm/${cid()}/transactions?${p}`);
			rows = [...rows, ...d.data];
			cursor = d.next_cursor;
			latency = d.latency_ms;
			if (!cursor) done = true;
		} catch (e: any) {
			if (e.status === 403) blocked = true;
			done = true;
		}
		loading = false;
	}

	onMount(() => {
		load();
		loadCashflow();
	});

	const filterChange = () => { loadCashflow(); if (!searchActive) load(true); };
</script>

<div class="page">
	<div class="bar searchbar">
		<input
			class="search"
			type="search"
			placeholder="Search transactions — try ‘coffee’, ‘ride home’, ‘big online shopping’…"
			bind:value={query}
			oninput={onSearchInput}
		/>
		<div class="seg" title="Hybrid fuses keyword + semantic; Vector is pure semantic; Text is keyword">
			<button type="button" class:on={mode === 'hybrid'} onclick={() => setMode('hybrid')}>Hybrid</button>
			<button type="button" class:on={mode === 'vector'} onclick={() => setMode('vector')}>Vector</button>
			<button type="button" class:on={mode === 'text'} onclick={() => setMode('text')}>Text</button>
		</div>
		{#if searchActive}
			<button type="button" class="clear" onclick={clearSearch}>clear</button>
			<span class="badge" style="margin-left:auto">
				<span class="dot"></span>{searching ? 'searching…' : searchMethod}
			</span>
		{/if}
	</div>
	{#if searchActive && searchNote}
		<div class="note mono">{searchNote}</div>
	{/if}

	<div class="bar">
		<select bind:value={institution} onchange={filterChange}>
			<option value="">All institutions</option>
			{#each Object.entries(SHORT_NAMES) as [dp, name]}
				<option value={dp}>{name}</option>
			{/each}
		</select>
		<select bind:value={category} onchange={filterChange}>
			<option value="">All categories</option>
			{#each Object.entries(CATEGORY_LABELS) as [k, v]}
				<option value={k}>{v}</option>
			{/each}
		</select>
		<select bind:value={indicator} onchange={filterChange}>
			<option value="">In + Out</option>
			<option value="debit">Money out</option>
			<option value="credit">Money in</option>
		</select>
		{#if latency && !searchActive}
			<span class="badge" style="margin-left:auto"><span class="dot"></span>indexed keyset query · {latency} ms</span>
		{/if}
	</div>

	{#if blocked}
		<div class="card" style="border-color: rgba(236,0,0,.4)">
			<b style="color:var(--red-soft)">403 Consent.InvalidScope</b> — this customer has no authorized
			<code>pfm</code> consent with <code>read_transactions</code>.
		</div>
	{:else}
		<div style="margin-bottom: 16px">
			<CashflowBars series={cashflow?.series ?? []} months={6} indicator={indicator} />
		</div>
		<div class="card" style="padding: 6px 16px">
			<table class="data">
				<thead>
					<tr><th>When</th><th>Description</th><th>Institution</th><th>Category</th><th>Method</th><th style="text-align:right">Amount</th></tr>
				</thead>
				<tbody>
					{#each rows as t}
						<tr>
							<td class="mono dim">{fmtDateTime(t.transaction_date)}</td>
							<td>
								<div class="desc">{t.enrichment?.merchant_normalized ?? t.description}</div>
								{#if t.foreign_currency_amount}
									<span class="chip" style="margin-top:3px">FX {t.foreign_currency_amount.currency} {fmtRM(t.foreign_currency_amount.amount)}</span>
								{/if}
								{#if !t.is_settled}<span class="chip warn" style="margin-top:3px">unsettled</span>{/if}
								{#if t.enrichment?.is_recurring}<span class="chip" style="margin-top:3px">↻ recurring</span>{/if}
							</td>
							<td>
								<span class="instname">
									<i style="background:{INSTITUTION_COLORS[t.account.dp_id] ?? '#666'}"></i>
									{SHORT_NAMES[t.account.dp_id] ?? t.account.institution_name}
								</span>
							</td>
							<td>
								<span class="cat" style="--c: {CATEGORY_COLORS[t.enrichment?.category] ?? '#666'}">
									{CATEGORY_LABELS[t.enrichment?.category] ?? t.enrichment?.category}
								</span>
							</td>
							<td class="mono dim">{t.transfer_submethod ?? (t.mcc ? `mcc ${t.mcc}` : '—')}</td>
							<td class="money amt" class:credit={t.credit_debit_indicator === 'credit'}>
								{t.credit_debit_indicator === 'credit' ? '+' : '−'}{fmtRM(t.amount.amount)}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
			<div class="footer">
				{#if loading || searching}
					<span class="mono dim">loading…</span>
				{:else if searchActive}
					<span class="mono dim">{rows.length} result{rows.length === 1 ? '' : 's'} · {searchMethod} · ranked by relevance</span>
				{:else if !done}
					<button type="button" class="loadmore" onclick={() => load()}>Load more</button>
					<span class="mono dim">{rows.length} shown</span>
				{:else if rows.length}
					<span class="mono dim">{rows.length} rows · end of result</span>
				{:else}
					<span class="mono dim">no transactions</span>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.bar { display: flex; gap: 10px; margin-bottom: 16px; align-items: center; }
	.searchbar { margin-bottom: 10px; }
	.search {
		flex: 1;
		min-width: 220px;
		padding: 9px 13px;
		border-radius: 9px;
		border: 1px solid var(--line);
		background: var(--card);
		color: var(--ink);
		font-size: 13px;
	}
	.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 9px; overflow: hidden; }
	.seg button {
		background: transparent;
		border: 0;
		color: var(--ink-dim);
		font-size: 12.5px;
		padding: 8px 14px;
		cursor: pointer;
		transition: background 0.14s, color 0.14s;
	}
	.seg button:not(:last-child) { border-right: 1px solid var(--line); }
	.seg button.on { background: linear-gradient(90deg, rgba(236, 0, 0, 0.18), rgba(236, 0, 0, 0.06)); color: var(--ink); }
	.clear {
		background: transparent;
		border: 1px solid var(--line);
		border-radius: 8px;
		color: var(--ink-dim);
		font-size: 12px;
		padding: 7px 12px;
		cursor: pointer;
	}
	.note { font-size: 11.5px; color: var(--amber); margin: -2px 0 14px; }
	.dim { color: var(--ink-faint); font-size: 11.5px; }
	.desc { font-size: 13.5px; }
	.instname { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; }
	.instname i { width: 8px; height: 8px; border-radius: 2.5px; display: inline-block; }
	.cat {
		font-size: 11.5px;
		color: var(--c);
		border: 1px solid color-mix(in srgb, var(--c) 40%, transparent);
		padding: 2px 8px;
		border-radius: 6px;
		white-space: nowrap;
	}
	.amt { text-align: right; font-size: 14.5px; white-space: nowrap; }
	.amt.credit { color: var(--green); }
	.footer { padding: 16px; text-align: center; display: flex; gap: 12px; align-items: center; justify-content: center; }
	.loadmore {
		background: linear-gradient(90deg, rgba(236, 0, 0, 0.16), rgba(236, 0, 0, 0.05));
		border: 1px solid color-mix(in srgb, var(--red) 35%, var(--line));
		color: var(--ink);
		font-size: 12.5px;
		font-weight: 500;
		padding: 8px 22px;
		border-radius: 9px;
		cursor: pointer;
		transition: filter 0.14s;
	}
	.loadmore:hover { filter: brightness(1.2); }
</style>
