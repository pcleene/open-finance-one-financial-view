<script lang="ts">
	// Reusable "How it works" flow explainer — a vertical timeline of the real
	// data & operations for a feature, each node collapsible to reveal the
	// underlying API call + MongoDB query end-to-end (for live demoing).
	import { highlight } from '$lib/queryFmt';

	export interface FlowStep {
		title: string;
		// api | mongo | event | txn | kafka | worker | logic — drives the accent colour
		tone?: string;
		tag?: string;
		desc: string;
		api?: { method: string; path: string };
		affects?: string; // how this step affects the consent
		file?: string;
		code?: string; // code snippet (syntax-highlighted)
		query?: string; // MongoDB query (syntax-highlighted, labelled separately)
		note?: string;
	}

	export interface FlowTab {
		label: string;
		subtitle?: string;
		footer?: string;
		steps: FlowStep[];
	}

	let {
		title,
		subtitle,
		footer,
		steps,
		tabs,
		onclose
	}: {
		title: string;
		subtitle?: string;
		footer?: string;
		steps?: FlowStep[];
		tabs?: FlowTab[];
		onclose: () => void;
	} = $props();

	let activeTab = $state(0);
	const flowSteps = $derived(tabs ? tabs[activeTab].steps : (steps ?? []));
	const flowSubtitle = $derived(tabs ? tabs[activeTab].subtitle : subtitle);
	const flowFooter = $derived(tabs ? tabs[activeTab].footer : footer);

	let open = $state<Record<number, boolean>>({});
	const allOpen = $derived(flowSteps.length > 0 && flowSteps.every((_, i) => open[i]));

	function toggle(i: number) {
		open = { ...open, [i]: !open[i] };
	}
	function toggleAll() {
		const next = !allOpen;
		open = Object.fromEntries(flowSteps.map((_, i) => [i, next]));
	}
	function setTab(i: number) {
		activeTab = i;
		open = {}; // collapse all when switching flows
	}

	const toneColor: Record<string, string> = {
		api: 'var(--blue)',
		mongo: 'var(--green)',
		event: 'var(--amber)',
		txn: '#c4b5fd',
		kafka: '#c4b5fd',
		worker: 'var(--ink-dim)',
		logic: 'var(--ink-dim)'
	};
	const tagLabels: Record<string, string> = {
		api: 'API', mongo: 'MongoDB', event: 'Change stream', txn: 'ACID txn',
		kafka: 'Kafka', worker: 'Worker', logic: 'Logic'
	};
	const tagFor = (s: FlowStep) => s.tag ?? tagLabels[s.tone ?? 'logic'] ?? 'Step';
</script>

<div
	class="hiw-backdrop"
	role="button"
	tabindex="0"
	onclick={onclose}
	onkeydown={(e) => e.key === 'Escape' && onclose()}
>
	<div class="hiw-modal" role="dialog" tabindex="0" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
		<div class="hiw-head">
			<div>
				<h3>{title}</h3>
				{#if flowSubtitle}<div class="hiw-sub">{flowSubtitle}</div>{/if}
			</div>
			<div class="hiw-actions">
				<button class="btn ghost" onclick={toggleAll}>{allOpen ? 'collapse all' : 'expand all'}</button>
				<button class="btn" onclick={onclose}>close</button>
			</div>
		</div>

		{#if tabs}
			<div class="hiw-tabs">
				{#each tabs as t, ti}
					<button class="hiw-tab" class:active={ti === activeTab} onclick={() => setTab(ti)}>{t.label}</button>
				{/each}
			</div>
		{/if}

		<ol class="hiw-flow">
			{#each flowSteps as s, i (activeTab + '-' + i)}
				{@const tint = toneColor[s.tone ?? 'logic'] ?? 'var(--ink-dim)'}
				<li class="hiw-step" class:open={open[i]} style="--tint: {tint}">
					<div class="hiw-rail">
						<span class="hiw-node">{String(i + 1).padStart(2, '0')}</span>
						{#if i < flowSteps.length - 1}<span class="hiw-conn"></span>{/if}
					</div>

					<div class="hiw-body">
						<button class="hiw-top" onclick={() => toggle(i)} aria-expanded={open[i] ? 'true' : 'false'}>
							<span class="hiw-title">{s.title}</span>
							<span class="hiw-tag mono">{tagFor(s)}</span>
							<span class="hiw-chev" class:rot={open[i]}>›</span>
						</button>
						<p class="hiw-desc">{s.desc}</p>

						{#if open[i]}
							<div class="hiw-drill">
								{#if s.api}
									<div class="hiw-api mono">
										<span class="hiw-method hiw-{s.api.method.toLowerCase()}">{s.api.method}</span>
										<span class="hiw-path">{s.api.path}</span>
									</div>
								{/if}
								{#if s.affects}
									<p class="hiw-affects"><span>consent effect</span>{s.affects}</p>
								{/if}
								{#if s.code}
									<div class="hiw-blk">
										<div class="hiw-blk-head">
											<span class="hiw-lbl">code</span>
											{#if s.file}<span class="hiw-file mono">{s.file}</span>{/if}
										</div>
										<pre class="hiw-code">{@html highlight(s.code)}</pre>
									</div>
								{/if}
								{#if s.query}
									<div class="hiw-blk">
										<div class="hiw-blk-head"><span class="hiw-lbl mongo">MongoDB</span></div>
										<pre class="hiw-code">{@html highlight(s.query)}</pre>
									</div>
								{/if}
								{#if s.note}<p class="hiw-note">{s.note}</p>{/if}
							</div>
						{/if}
					</div>
				</li>
			{/each}
		</ol>

		{#if flowFooter}<p class="hiw-foot mono">{flowFooter}</p>{/if}
	</div>
</div>

<style>
	.hiw-backdrop {
		position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); backdrop-filter: blur(3px);
		display: flex; align-items: center; justify-content: center; z-index: 70; padding: 24px;
	}
	.hiw-modal {
		background: linear-gradient(180deg, var(--card-2), var(--card));
		border: 1px solid var(--line-2); border-radius: 18px; width: min(760px, 100%);
		max-height: 88vh; overflow: auto; padding: 24px 26px;
		box-shadow: 0 30px 80px rgba(0, 0, 0, 0.5);
	}
	.hiw-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 22px; }
	.hiw-head h3 { font-family: var(--serif); font-weight: 500; font-size: 20px; letter-spacing: 0; text-transform: none; color: var(--ink); margin: 0; }
	.hiw-sub { font-size: 12px; color: var(--ink-dim); margin-top: 6px; line-height: 1.5; max-width: 52ch; }
	.hiw-actions { display: flex; gap: 8px; flex-shrink: 0; }
	.hiw-actions .btn { font-size: 11px; padding: 6px 12px; }
	.btn.ghost { background: transparent; }

	.hiw-tabs { display: flex; gap: 4px; margin-bottom: 18px; border-bottom: 1px solid var(--line); }
	.hiw-tab {
		background: transparent; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px;
		color: var(--ink-dim); font-family: var(--sans); font-weight: 600; font-size: 12.5px;
		padding: 8px 14px; cursor: pointer; transition: color 0.15s, border-color 0.15s;
	}
	.hiw-tab:hover { color: var(--ink); }
	.hiw-tab.active { color: var(--ink); border-bottom-color: var(--red); }

	.hiw-flow { list-style: none; margin: 0; padding: 0; }
	.hiw-step { display: flex; gap: 16px; }

	.hiw-rail { display: flex; flex-direction: column; align-items: center; flex: 0 0 auto; }
	.hiw-node {
		width: 34px; height: 34px; border-radius: 11px; display: flex; align-items: center; justify-content: center;
		font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--bg);
		background: var(--tint); box-shadow: 0 2px 10px color-mix(in srgb, var(--tint) 40%, transparent);
	}
	.hiw-conn { width: 2px; flex: 1; min-height: 14px; margin: 4px 0; background: linear-gradient(var(--tint), var(--line)); opacity: 0.5; }

	.hiw-body { flex: 1; min-width: 0; padding-bottom: 20px; }
	.hiw-top {
		display: flex; align-items: center; gap: 10px; width: 100%; background: transparent;
		border: 0; padding: 5px 0 0; cursor: pointer; text-align: left;
	}
	.hiw-title { font-size: 14px; font-weight: 600; color: var(--ink); }
	.hiw-tag {
		font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.08em; padding: 2px 7px;
		border-radius: 999px; color: var(--tint); border: 1px solid color-mix(in srgb, var(--tint) 35%, var(--line));
		background: color-mix(in srgb, var(--tint) 9%, transparent); white-space: nowrap;
	}
	.hiw-chev { margin-left: auto; color: var(--ink-faint); font-size: 17px; line-height: 1; transition: transform 0.15s; }
	.hiw-chev.rot { transform: rotate(90deg); color: var(--tint); }
	.hiw-desc { font-size: 12.5px; color: var(--ink-dim); line-height: 1.6; margin: 7px 0 0; }

	.hiw-drill { margin-top: 12px; display: flex; flex-direction: column; gap: 11px; }
	.hiw-api { display: flex; align-items: center; gap: 9px; font-size: 11.5px; flex-wrap: wrap; }
	.hiw-method { font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 6px; letter-spacing: 0.04em; }
	.hiw-get { background: rgba(62, 207, 142, 0.16); color: var(--green); }
	.hiw-post { background: rgba(95, 168, 211, 0.18); color: var(--blue); }
	.hiw-put { background: rgba(255, 182, 39, 0.16); color: var(--amber); }
	.hiw-path { color: var(--ink); }

	.hiw-affects { font-size: 11.5px; color: var(--ink-dim); line-height: 1.55; margin: 0;
		border-left: 2px solid color-mix(in srgb, var(--tint) 55%, var(--line)); padding-left: 11px; }
	.hiw-affects span { display: inline-block; color: var(--tint); font-weight: 700; font-size: 8.5px;
		text-transform: uppercase; letter-spacing: 0.1em; margin-right: 8px; }

	.hiw-blk { display: flex; flex-direction: column; gap: 5px; }
	.hiw-blk-head { display: flex; align-items: center; gap: 10px; }
	.hiw-lbl { font-size: 8.5px; text-transform: uppercase; letter-spacing: 0.12em; font-weight: 700; color: var(--ink-faint); }
	.hiw-lbl.mongo { color: var(--green); }
	.hiw-file { font-size: 10px; color: var(--ink-faint); }
	.hiw-code {
		margin: 0; padding: 13px 15px; font-family: var(--mono); font-size: 11px; line-height: 1.6;
		color: var(--ink-dim); white-space: pre; overflow: auto; max-height: 360px;
		background: var(--bg); border: 1px solid var(--line); border-radius: 11px;
	}
	.hiw-note { font-size: 11px; color: var(--ink-faint); line-height: 1.6; margin: 0; border-left: 2px solid color-mix(in srgb, var(--tint) 50%, var(--line)); padding-left: 11px; }
	.hiw-foot { font-size: 10.5px; color: var(--ink-faint); line-height: 1.65; margin: 6px 0 0; border-top: 1px solid var(--line); padding-top: 16px; }

	:global(.hiw-code .tk-op) { color: #c4b5fd; font-weight: 600; }
	:global(.hiw-code .tk-key) { color: #7dd3fc; }
	:global(.hiw-code .tk-str) { color: #86efac; }
	:global(.hiw-code .tk-num) { color: #fdba74; }
	:global(.hiw-code .tk-bool) { color: #fca5a5; }
	:global(.hiw-code .tk-dim) { color: #fbbf24; font-style: italic; }
	:global(.hiw-code .tk-db) { color: #e2b74b; font-weight: 700; }
	:global(.hiw-code .tk-comment) { color: var(--ink-faint); font-style: italic; }
</style>
