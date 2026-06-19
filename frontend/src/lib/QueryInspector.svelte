<script lang="ts">
	import { queryLog, type QueryEntry } from '$lib/stores/queryLog.svelte';
	import { fmt, toShell, highlight } from '$lib/queryFmt';

	let open = $state(false);
	let copied = $state<string | null>(null);

	function summary(entry: QueryEntry): string {
		const n = entry.result_count;
		if (n === undefined || n === null) return '';
		return `${n} doc${n !== 1 ? 's' : ''} returned`;
	}

	function timeAgo(ts: number): string {
		const s = Math.floor(Date.now() / 1000 - ts);
		if (s < 5) return 'just now';
		if (s < 60) return `${s}s ago`;
		return `${Math.floor(s / 60)}m ago`;
	}

	async function copy(key: string, code: string) {
		try {
			await navigator.clipboard.writeText(code);
			copied = key;
			setTimeout(() => { if (copied === key) copied = null; }, 1500);
		} catch { /* ignore */ }
	}
</script>

<button class="qi-fab" onclick={() => (open = !open)} title="MongoDB Query Inspector" aria-label="Query Inspector">
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
		<path stroke-linecap="round" stroke-linejoin="round" d="M17.25 6.75 22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3-4.5 16.5" />
	</svg>
	{#if queryLog.count > 0}<span class="qi-fab-badge">{queryLog.count > 99 ? '99+' : queryLog.count}</span>{/if}
</button>

{#if open}
	<button class="qi-backdrop" onclick={() => (open = false)} tabindex="-1" aria-label="Close inspector"></button>
	<div class="qi-drawer">
		<div class="qi-header">
			<div class="qi-title">
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M17.25 6.75 22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3-4.5 16.5" /></svg>
				<div>
					<b>Query Inspector</b>
					<span>live db.collection operations · result document</span>
				</div>
			</div>
			<div class="qi-actions">
				<button class="qi-textbtn" onclick={() => queryLog.clear()}>Clear</button>
				<button class="qi-iconbtn" onclick={() => (open = false)} aria-label="Close">✕</button>
			</div>
		</div>

		<div class="qi-list">
			{#if queryLog.entries.length === 0}
				<div class="qi-empty">
					<div class="qi-empty-mark">{`{ }`}</div>
					<p>No queries captured yet</p>
					<small>Navigate or search to see live MongoDB operations here</small>
				</div>
			{:else}
				{#each queryLog.entries as entry, i}
					{@const queryCode = toShell(entry)}
					{@const hasResult = entry.result !== undefined && entry.result !== null}
					{@const sum = summary(entry)}
					<div class="qi-card">
						<div class="qi-card-head">
							<div class="qi-card-left">
								<span class="qi-op qi-op-{entry.operation}">{entry.operation}</span>
								<span class="qi-coll">{entry.collection}</span>
								{#if sum}<span class="qi-arrow">→</span><span class="qi-summary">{sum}</span>{/if}
							</div>
							<div class="qi-card-right">
								<span class="qi-dur" class:slow={entry.duration_ms > 200} class:mid={entry.duration_ms > 50 && entry.duration_ms <= 200}>{entry.duration_ms.toFixed(0)} ms</span>
								<span class="qi-ago">{timeAgo(entry.ts)}</span>
							</div>
						</div>

						<div class="qi-split" class:single={!hasResult}>
							<div class="qi-pane">
								<div class="qi-pane-label query">
									<span>Query</span>
									<button class="qi-copy" onclick={() => copy(`q${i}`, queryCode)} title="Copy query">{copied === `q${i}` ? '✓' : '⧉'}</button>
								</div>
								<div class="qi-code-scroll"><pre class="qi-code">{@html highlight(queryCode)}</pre></div>
							</div>
							{#if hasResult}
								<div class="qi-pane">
									<div class="qi-pane-label doc">
										<span>Sample document</span>
										<button class="qi-copy" onclick={() => copy(`d${i}`, fmt(entry.result))} title="Copy document">{copied === `d${i}` ? '✓' : '⧉'}</button>
									</div>
									<div class="qi-code-scroll"><pre class="qi-code">{@html highlight(fmt(entry.result))}</pre></div>
								</div>
							{/if}
						</div>

						<div class="qi-card-foot mono">{entry.endpoint}</div>
					</div>
				{/each}
			{/if}
		</div>

		<div class="qi-foot">
			<span><i class="qi-live"></i> live capture</span>
			<span>{queryLog.count} {queryLog.count === 1 ? 'query' : 'queries'}</span>
		</div>
	</div>
{/if}

<style>
	.qi-fab {
		position: fixed; bottom: 22px; right: 22px; z-index: 60;
		width: 46px; height: 46px; border-radius: 50%;
		display: flex; align-items: center; justify-content: center;
		background: linear-gradient(145deg, var(--card-2), var(--card));
		border: 1px solid var(--line); color: var(--ink-dim);
		box-shadow: 0 6px 22px rgba(0, 0, 0, 0.45); cursor: pointer;
		transition: transform 0.15s, color 0.15s, border-color 0.15s;
	}
	.qi-fab:hover { transform: scale(1.06); color: var(--green); border-color: color-mix(in srgb, var(--green) 45%, var(--line)); }
	.qi-fab svg { width: 19px; height: 19px; }
	.qi-fab-badge {
		position: absolute; top: -3px; right: -3px; min-width: 18px; height: 18px;
		padding: 0 4px; border-radius: 9px; background: var(--green); color: #04130c;
		font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center;
		font-family: var(--mono); border: 2px solid var(--bg, #0c0c0f);
	}
	.qi-backdrop { position: fixed; inset: 0; z-index: 60; background: rgba(0, 0, 0, 0.5); backdrop-filter: blur(2px); border: 0; cursor: default; }
	.qi-drawer {
		position: fixed; top: 0; right: 0; bottom: 0; z-index: 61;
		width: min(1180px, 95vw); display: flex; flex-direction: column;
		background: linear-gradient(180deg, var(--card), var(--card-2));
		border-left: 1px solid var(--line); box-shadow: -16px 0 40px rgba(0, 0, 0, 0.5);
		animation: qi-in 0.22s cubic-bezier(0.2, 0.7, 0.2, 1);
	}
	@keyframes qi-in { from { transform: translateX(24px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

	.qi-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--line); }
	.qi-title { display: flex; align-items: center; gap: 11px; }
	.qi-title svg { width: 26px; height: 26px; padding: 5px; border-radius: 8px; background: rgba(62, 207, 142, 0.12); color: var(--green); }
	.qi-title b { display: block; font-size: 13.5px; color: var(--ink); }
	.qi-title span { display: block; font-size: 10.5px; color: var(--ink-faint); margin-top: 1px; }
	.qi-actions { display: flex; align-items: center; gap: 6px; }
	.qi-textbtn { background: transparent; border: 1px solid var(--line); border-radius: 7px; color: var(--ink-dim); font-size: 11px; padding: 5px 10px; cursor: pointer; }
	.qi-iconbtn { width: 28px; height: 28px; border-radius: 7px; background: transparent; border: 1px solid var(--line); color: var(--ink-dim); cursor: pointer; }
	.qi-textbtn:hover, .qi-iconbtn:hover { color: var(--ink); }

	.qi-list { flex: 1; min-height: 0; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
	.qi-empty { margin: auto; text-align: center; color: var(--ink-faint); }
	.qi-empty-mark { font-family: var(--mono); font-size: 28px; color: var(--green); opacity: 0.5; margin-bottom: 10px; }
	.qi-empty p { font-size: 13px; color: var(--ink-dim); margin: 0 0 4px; }
	.qi-empty small { font-size: 11px; }

	.qi-card { border: 1px solid var(--line); border-radius: 11px; overflow: hidden; background: rgba(255, 255, 255, 0.015); flex-shrink: 0; }
	.qi-card-head { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; border-bottom: 1px solid var(--line); }
	.qi-card-left { display: flex; align-items: center; gap: 9px; }
	.qi-op { font-family: var(--mono); font-size: 10px; padding: 3px 7px; border-radius: 5px; background: rgba(148, 163, 184, 0.14); color: var(--ink-dim); }
	.qi-op-aggregate { background: rgba(139, 92, 246, 0.16); color: #c4b5fd; }
	.qi-op-find { background: rgba(6, 182, 212, 0.16); color: #67e8f9; }
	.qi-op-find_one { background: rgba(59, 130, 246, 0.16); color: #93c5fd; }
	.qi-op-count_documents { background: rgba(245, 158, 11, 0.16); color: #fcd34d; }
	.qi-coll { font-family: var(--mono); font-size: 11.5px; color: var(--ink-dim); }
	.qi-arrow { color: var(--ink-faint); font-size: 11px; }
	.qi-summary { font-family: var(--mono); font-size: 10px; color: var(--green); background: rgba(62, 207, 142, 0.1); padding: 2px 7px; border-radius: 5px; }
	.qi-card-right { display: flex; align-items: center; gap: 12px; }
	.qi-dur { font-family: var(--mono); font-size: 10.5px; color: var(--green); }
	.qi-dur.mid { color: var(--amber); }
	.qi-dur.slow { color: var(--red-soft); }
	.qi-ago { font-size: 10px; color: var(--ink-faint); }

	.qi-split { display: flex; height: clamp(300px, 46vh, 520px); }
	.qi-pane { flex: 1 1 50%; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
	.qi-split.single .qi-pane { flex: 1 1 100%; }
	.qi-pane + .qi-pane { border-left: 1px solid var(--line); }
	.qi-pane-label { display: flex; align-items: center; gap: 6px; padding: 5px 11px; font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.09em; color: var(--ink-faint); border-bottom: 1px solid var(--line); }
	.qi-pane-label.query { background: rgba(139, 92, 246, 0.05); }
	.qi-pane-label.doc { background: rgba(62, 207, 142, 0.05); }
	.qi-copy { margin-left: auto; background: transparent; border: 0; color: var(--ink-faint); cursor: pointer; font-size: 12px; }
	.qi-copy:hover { color: var(--ink); }
	.qi-code-scroll { flex: 1; min-height: 0; overflow: auto; }
	.qi-code { padding: 11px 13px; margin: 0; font-size: 11px; line-height: 1.6; font-family: var(--mono); color: var(--ink-dim); white-space: pre; tab-size: 2; }
	.qi-card-foot { padding: 6px 12px; font-size: 10px; color: var(--ink-faint); border-top: 1px solid var(--line); background: rgba(0, 0, 0, 0.12); }

	.qi-foot { display: flex; align-items: center; justify-content: space-between; padding: 9px 18px; border-top: 1px solid var(--line); font-size: 10.5px; color: var(--ink-faint); }
	.qi-foot span { display: inline-flex; align-items: center; gap: 7px; }
	.qi-live { width: 7px; height: 7px; border-radius: 50%; background: var(--green); animation: qi-pulse 1.4s infinite; }
	@keyframes qi-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

	:global(.qi-code .tk-op) { color: #c4b5fd; font-weight: 600; }
	:global(.qi-code .tk-key) { color: #7dd3fc; }
	:global(.qi-code .tk-str) { color: #86efac; }
	:global(.qi-code .tk-num) { color: #fdba74; }
	:global(.qi-code .tk-bool) { color: #fca5a5; }
	:global(.qi-code .tk-dim) { color: #fbbf24; font-style: italic; }
	:global(.qi-code .tk-db) { color: #e2b74b; font-weight: 700; }
	:global(.qi-code .tk-comment) { color: var(--ink-faint); font-style: italic; }
</style>
