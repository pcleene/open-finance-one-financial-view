<script lang="ts">
	import { onMount } from 'svelte';
	import { api, cid, fmtRM, fmtDateTime, INSTITUTION_COLORS, SHORT_NAMES, CATEGORY_LABELS } from '$lib/api';

	let data: any = $state(null);
	let budgets: any = $state(null);
	let err: string = $state('');
	let displayNet = $state(0);

	function countUp(target: number) {
		const t0 = performance.now();
		const dur = 900;
		const tick = (t: number) => {
			const k = Math.min(1, (t - t0) / dur);
			displayNet = target * (1 - Math.pow(1 - k, 3));
			if (k < 1) requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	}

	onMount(async () => {
		try {
			data = await api(`/one-view/${cid()}`);
			countUp(parseFloat(data.profile.summary.net_position.amount));
			// budgets ride the One View response now — same profile read feeds the
			// budget transactions aggregate, so the home page is a single profile read.
			budgets = data.budgets ?? null;
		} catch (e: any) {
			err = e.message;
		}
	});

	const byInstitution = $derived.by(() => {
		if (!data) return [];
		const groups: Record<string, any[]> = {};
		for (const a of data.profile.accounts) (groups[a.dp_id] ??= []).push(a);
		return Object.entries(groups);
	});

	function daysLeft(iso: string) {
		return Math.max(0, Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000));
	}
	function balanceOf(a: any) {
		const b = a.balances?.current_balance;
		if (!b) return null;
		const v = parseFloat(b.amount);
		return b.credit_debit_indicator === 'debit' ? -v : v;
	}
</script>

<div class="page">
	{#if err}
		<div class="card">API error: {err} — are the backend services running?</div>
	{:else if !data}
		<div class="grid" style="grid-template-columns: 1fr">
			<div class="skeleton" style="height:160px"></div>
			<div class="skeleton" style="height:380px"></div>
		</div>
	{:else}
		<section class="hero">
			<div>
				<div class="hello">Salam sejahtera,</div>
				<div class="name">{data.profile.customer.preferred_name}</div>
				<div class="netlabel">NET POSITION · ALL INSTITUTIONS</div>
				<div class="net money" class:neg={displayNet < 0}>
					<span class="cur">RM</span>{fmtRM(displayNet, 2)}
				</div>
				<div class="meta">
					{data.profile.summary.institutions_linked.length} institutions ·
					{data.profile.summary.external_account_count} external accounts ·
					{data.profile.summary.active_consent_count} active consents
				</div>
			</div>
			<div class="herobadges">
				<div class="badge"><span class="dot"></span>1 document read · {data.latency_ms.profile_read} ms</div>
				<div class="explain mono">
					customer_profiles.find_one(_id) + $filter on embedded consent boxes —
					consent enforced inside the read
				</div>
			</div>
		</section>

		{#if budgets?.budgets?.some((b: any) => b.alert)}
			<div class="alerts">
				{#each budgets.budgets.filter((b: any) => b.alert) as b}
					<div class="alert">
						⚠ {CATEGORY_LABELS[b.category] ?? b.category} budget at {Math.round(b.pct * 100)}%
						(RM {fmtRM(b.spent, 0)} of RM {fmtRM(b.monthly_limit, 0)})
					</div>
				{/each}
			</div>
		{/if}

		<div class="cols">
			<section>
				{#each byInstitution as [dp, accounts], i}
					<div class="inst" style="--brand: {INSTITUTION_COLORS[dp] ?? '#888'}; animation-delay: {i * 70}ms">
						<div class="insthead">
							<span class="swatch"></span>
							<b>{SHORT_NAMES[dp] ?? accounts[0].institution_name}</b>
							{#if dp === 'Acme-INTERNAL'}<span class="chip">internal</span>{/if}
						</div>
						{#each accounts as a}
							<div class="acct">
								<div class="acctinfo">
									<div class="acctname">{a.account_name}</div>
									<div class="acctnum mono">{a.account_number_masked} · {a.subtype.replace('_', ' ')}</div>
								</div>
								<div class="acctright">
									<div class="bal money" class:neg={(balanceOf(a) ?? 0) < 0}>
										{#if balanceOf(a) !== null}RM {fmtRM(balanceOf(a))}{:else}—{/if}
									</div>
									<div class="boxes">
										{#if a.is_internal}
											<span class="chip">core banking</span>
										{:else}
											{#each a.consents.filter((c: any) => c.status === 'authorized') as c}
												<span class="chip ok" title="consent {c.consent_id}">
													{c.consent_purpose === 'credit_underwriting' ? 'underwriting' : c.consent_purpose}
													· {daysLeft(c.expiration_datetime)}d
												</span>
											{/each}
										{/if}
									</div>
								</div>
							</div>
						{/each}
					</div>
				{/each}
			</section>

			<section class="card recent">
				<h3>Recent activity · cross-bank</h3>
				{#each data.recent_activity as t}
					<div class="txn">
						<div class="tdot" style="background: {INSTITUTION_COLORS[t.account.dp_id] ?? '#666'}"></div>
						<div class="tmain">
							<div class="tdesc">{t.enrichment?.merchant_normalized ?? t.description}</div>
							<div class="tsub mono">
								{SHORT_NAMES[t.account.dp_id] ?? t.account.institution_name} · {fmtDateTime(t.transaction_date)}
								{#if !t.is_settled}<span class="chip warn" style="margin-left:6px">pending</span>{/if}
							</div>
						</div>
						<div class="tamt money" class:credit={t.credit_debit_indicator === 'credit'}>
							{t.credit_debit_indicator === 'credit' ? '+' : '−'}RM {fmtRM(t.amount.amount)}
						</div>
					</div>
				{/each}
			</section>
		</div>
	{/if}
</div>

<style>
	.hero {
		display: flex;
		justify-content: space-between;
		align-items: flex-end;
		padding: 14px 6px 30px;
	}
	.hello { color: var(--ink-dim); font-size: 14px; }
	.name { font-family: var(--serif); font-size: 34px; font-weight: 500; margin-bottom: 22px; }
	.netlabel { font-size: 10.5px; letter-spacing: 0.18em; color: var(--ink-faint); font-weight: 600; }
	.net { font-size: 58px; font-weight: 400; line-height: 1.15; letter-spacing: -0.01em; }
	.net .cur { font-size: 26px; color: var(--ink-dim); margin-right: 8px; }
	.net.neg { color: var(--red-soft); }
	.meta { color: var(--ink-dim); font-size: 13px; margin-top: 6px; }
	.herobadges { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; max-width: 320px; }
	.explain { font-size: 10.5px; color: var(--ink-faint); text-align: right; line-height: 1.6; }

	.alerts { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
	.alert {
		font-size: 12.5px;
		color: var(--amber);
		border: 1px solid rgba(255, 182, 39, 0.3);
		background: rgba(255, 182, 39, 0.06);
		padding: 8px 14px;
		border-radius: 10px;
	}

	.cols { display: grid; grid-template-columns: 1.5fr 1fr; gap: 18px; align-items: start; }

	.inst {
		background: linear-gradient(180deg, var(--card-2), var(--card));
		border: 1px solid var(--line);
		border-left: 3px solid var(--brand);
		border-radius: 14px;
		padding: 16px 18px;
		margin-bottom: 14px;
		animation: pagein 0.5s both;
	}
	.insthead { display: flex; align-items: center; gap: 9px; margin-bottom: 6px; font-size: 14px; }
	.swatch { width: 9px; height: 9px; border-radius: 3px; background: var(--brand); }

	.acct {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 11px 0;
		border-top: 1px solid var(--line);
	}
	.acct:first-of-type { border-top: 0; }
	.acctname { font-weight: 500; font-size: 14px; }
	.acctnum { color: var(--ink-faint); font-size: 11.5px; margin-top: 2px; }
	.acctright { text-align: right; }
	.bal { font-size: 17px; }
	.bal.neg { color: var(--red-soft); }
	.boxes { display: flex; gap: 5px; justify-content: flex-end; margin-top: 4px; flex-wrap: wrap; }

	.recent { position: sticky; top: 86px; }
	.txn { display: flex; align-items: center; gap: 11px; padding: 9px 0; border-bottom: 1px solid var(--line); }
	.txn:last-child { border-bottom: 0; }
	.tdot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
	.tmain { flex: 1; min-width: 0; }
	.tdesc { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.tsub { font-size: 10.5px; color: var(--ink-faint); margin-top: 2px; }
	.tamt { font-size: 14px; white-space: nowrap; }
	.tamt.credit { color: var(--green); }
</style>
