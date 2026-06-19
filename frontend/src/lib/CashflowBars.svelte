<script lang="ts">
	import { fmtRM } from '$lib/api';

	let {
		series = [],
		title = 'Money in / Money out',
		months = 6,
		indicator = ''
	}: {
		series?: any[];
		title?: string;
		months?: number;
		indicator?: string;
	} = $props();

	const cfMax = $derived(
		Math.max(1, ...(series ?? []).flatMap((m: any) => [parseFloat(m.money_in), parseFloat(m.money_out)]))
	);
</script>

<div class="card">
	<h3>{title} · {months} months</h3>
	<div class="bars">
		{#each series ?? [] as m}
			<div class="bargroup">
				<div class="barpair">
					<div
						class="bar in"
						class:dim={indicator === 'debit'}
						style="height: {(parseFloat(m.money_in) / cfMax) * 120}px"
						title="in RM {fmtRM(m.money_in)}"
					></div>
					<div
						class="bar out"
						class:dim={indicator === 'credit'}
						style="height: {(parseFloat(m.money_out) / cfMax) * 120}px"
						title="out RM {fmtRM(m.money_out)}"
					></div>
				</div>
				<div class="net mono" class:neg={parseFloat(m.net) < 0}>
					{parseFloat(m.net) > 0 ? '+' : ''}{fmtRM(m.net, 0)}
				</div>
				<div class="mlabel mono">{m.month.slice(2)}</div>
			</div>
		{/each}
		{#if !(series ?? []).length}<span class="mono empty">no cashflow in range</span>{/if}
	</div>
	<div class="legend mono"><i class="sw in"></i> money in <i class="sw out"></i> money out</div>
</div>

<style>
	.bars { display: flex; gap: 14px; align-items: flex-end; padding: 12px 4px 0; min-height: 124px; }
	.bargroup { flex: 1; text-align: center; }
	.barpair { display: flex; gap: 4px; align-items: flex-end; justify-content: center; height: 124px; }
	.bar { width: 16px; border-radius: 4px 4px 0 0; transition: height 0.6s cubic-bezier(0.2, 0.7, 0.2, 1), opacity 0.2s; }
	.bar.in { background: linear-gradient(180deg, var(--green), rgba(62, 207, 142, 0.4)); }
	.bar.out { background: linear-gradient(180deg, var(--red), rgba(236, 0, 0, 0.35)); }
	.bar.dim { opacity: 0.18; }
	.net { font-size: 10px; margin-top: 6px; color: var(--green); }
	.net.neg { color: var(--red-soft); }
	.mlabel { font-size: 10px; color: var(--ink-faint); margin-top: 2px; }
	.legend { font-size: 10.5px; color: var(--ink-faint); margin-top: 12px; }
	.sw { display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin: 0 5px 0 10px; vertical-align: -1px; }
	.sw.in { background: var(--green); }
	.sw.out { background: var(--red); }
	.empty { color: var(--ink-faint); font-size: 11px; align-self: center; }
</style>
