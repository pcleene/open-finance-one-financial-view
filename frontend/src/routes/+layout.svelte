<script lang="ts">
	import '../app.css';
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { api, customer } from '$lib/api';
	import QueryInspector from '$lib/QueryInspector.svelte';
	import { queryLog } from '$lib/stores/queryLog.svelte';

	type Persona = {
		customer_id: string;
		full_name: string;
		preferred_name: string;
		segment: string;
		external_account_count: number;
		active_consent_count: number;
	};
	let { children } = $props();
	let personas: Persona[] = $state([]);
	let menuOpen = $state(false);
	let switcherOpen = $state(false);

	async function loadCustomers() {
		personas = (await api('/customers')).customers;
	}
	onMount(loadCustomers);

	// reset the inspector when the view (route) or customer changes
	$effect(() => {
		void $customer;
		void $page.url.pathname;
		queryLog.clear();
	});

	const customerNav = [
		{ href: '/', label: 'One View', icon: '◉' },
		{ href: '/transactions', label: 'Transactions', icon: '⇅' },
		{ href: '/insights', label: 'Insights', icon: '◐' }
	];
	const staffNav = [
		{ href: '/underwriting', label: 'Underwriting', icon: '§' },
		{ href: '/ops', label: 'Scale Ops', icon: '∿' },
		{ href: '/simulation', label: 'Simulation', icon: '⚡' }
	];
	const allNav = [...customerNav, ...staffNav, { href: '/profile', label: 'Manage Profile', icon: '☰' }];
	const crumb = $derived(allNav.find((n) => n.href === $page.url.pathname)?.label ?? '');
	const me = $derived(personas.find((p) => p.customer_id === $customer));

	function pick(id: string) {
		customer.set(id);
		switcherOpen = false;
		menuOpen = false;
	}
</script>

<div class="shell">
	<aside>
		<div class="brand">
			<div class="logo">Acme</div>
			<div class="product">ONE FINANCIAL VIEW<span>POC · MongoDB Atlas</span></div>
		</div>
		<nav>
			{#each customerNav as n}
				<a href={n.href} class:active={$page.url.pathname === n.href}>
					<span class="icon">{n.icon}</span>{n.label}
				</a>
			{/each}
			<div class="navsection mono">Acme staff view</div>
			{#each staffNav as n}
				<a href={n.href} class:active={$page.url.pathname === n.href}>
					<span class="icon">{n.icon}</span>{n.label}
				</a>
			{/each}
		</nav>
		<div class="foot mono">
			Open Finance Platform v1.2.2<br />Data Consumer: DC-ACME-001-2A8E
		</div>
	</aside>

	<div class="main">
		<header>
			<div class="crumb">{crumb}</div>
			<div class="userbox">
				<button class="userbtn" onclick={() => (menuOpen = !menuOpen)}>
					<span class="avatar">{(me?.preferred_name ?? 'C').charAt(0)}</span>
					<span class="uinfo">
						<b>{me?.preferred_name ?? '—'}</b>
						<small>{me?.segment ?? ''}</small>
					</span>
					<span class="chev">▾</span>
				</button>
				{#if menuOpen}
					<button class="menubackdrop" onclick={() => (menuOpen = false)} aria-label="close menu" tabindex="-1"></button>
					<div class="usermenu">
						<a href="/profile" onclick={() => (menuOpen = false)}>Manage profile &amp; consents</a>
						<div class="mdiv"></div>
						<button class="demoswitch" onclick={() => { switcherOpen = true; menuOpen = false; }}>
							<span class="demotag">demo only</span> Switch user
						</button>
					</div>
				{/if}
			</div>
		</header>
		{#key $customer + $page.url.pathname}
			{@render children()}
		{/key}
	</div>
</div>

{#if switcherOpen}
	<div class="overlay" onclick={() => (switcherOpen = false)} role="presentation">
		<div class="switcher" onclick={(e) => e.stopPropagation()} role="dialog" tabindex="-1">
			<div class="shead">
				<b>Switch user</b><span class="demotag">demo only</span>
				<button class="x" onclick={() => (switcherOpen = false)} aria-label="close">✕</button>
			</div>
			<div class="ulist">
				{#each personas as p}
					<button class="urow" class:cur={p.customer_id === $customer} onclick={() => pick(p.customer_id)}>
						<span class="avatar">{p.preferred_name.charAt(0)}</span>
						<span class="urinfo">
							<b>{p.full_name}</b>
							<small>{p.segment} · {p.external_account_count} accounts tracked · {p.active_consent_count} active consents</small>
						</span>
						{#if p.customer_id === $customer}<span class="curtag mono">current</span>{/if}
					</button>
				{/each}
			</div>
		</div>
	</div>
{/if}

<QueryInspector />

<style>
	.shell { display: flex; min-height: 100vh; }

	aside {
		width: 232px; flex-shrink: 0; border-right: 1px solid var(--line);
		padding: 26px 18px; display: flex; flex-direction: column;
		position: sticky; top: 0; height: 100vh; background: rgba(12, 12, 15, 0.6);
	}
	.brand { display: flex; align-items: center; gap: 10px; padding: 0 8px 26px; }
	.logo {
		font-family: var(--sans); font-weight: 700; font-size: 15px; letter-spacing: 0.04em;
		color: #fff; background: var(--red); border-radius: 8px; padding: 7px 9px;
	}
	.product { font-size: 10px; font-weight: 700; letter-spacing: 0.13em; line-height: 1.5; }
	.product span { display: block; color: var(--ink-faint); font-weight: 500; letter-spacing: 0.08em; }

	nav { display: flex; flex-direction: column; gap: 3px; }
	nav a {
		display: flex; align-items: center; gap: 11px; padding: 10px 12px; border-radius: 10px;
		font-size: 13.5px; font-weight: 500; color: var(--ink-dim); transition: all 0.14s;
	}
	nav a .icon { width: 18px; text-align: center; opacity: 0.8; }
	nav a:hover { color: var(--ink); background: var(--card); }
	nav a.active {
		color: var(--ink);
		background: linear-gradient(90deg, rgba(236, 0, 0, 0.16), rgba(236, 0, 0, 0.04));
		border-left: 2px solid var(--red); border-radius: 4px 10px 10px 4px;
	}
	.navsection {
		font-size: 9px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--amber);
		margin: 16px 0 6px; padding: 0 12px; border-top: 1px solid var(--line); padding-top: 14px;
	}

	.foot { margin-top: auto; font-size: 10px; color: var(--ink-faint); line-height: 1.7; padding: 0 8px; }

	.main { flex: 1; min-width: 0; }
	header {
		display: flex; align-items: center; justify-content: space-between; padding: 14px 36px;
		border-bottom: 1px solid var(--line); position: sticky; top: 0;
		backdrop-filter: blur(14px); background: rgba(12, 12, 15, 0.75); z-index: 20;
	}
	.crumb { font-size: 16px; font-weight: 600; }

	.userbox { position: relative; }
	.userbtn {
		display: flex; align-items: center; gap: 10px; background: var(--card);
		border: 1px solid var(--line); border-radius: 12px; padding: 6px 12px 6px 6px; cursor: pointer;
	}
	.userbtn:hover { border-color: var(--line-2, var(--line)); }
	.avatar {
		width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
		display: flex; align-items: center; justify-content: center;
		background: linear-gradient(135deg, var(--red), #8a0000); color: #fff;
		font-weight: 700; font-size: 13px;
	}
	.uinfo { display: flex; flex-direction: column; line-height: 1.2; text-align: left; }
	.uinfo b { font-size: 13px; color: var(--ink); }
	.uinfo small { font-size: 10px; color: var(--ink-faint); text-transform: capitalize; }
	.chev { color: var(--ink-faint); font-size: 11px; }

	.menubackdrop { position: fixed; inset: 0; z-index: 30; background: transparent; border: 0; cursor: default; }
	.usermenu {
		position: absolute; right: 0; top: calc(100% + 8px); z-index: 31; width: 240px;
		background: var(--card-2); border: 1px solid var(--line); border-radius: 12px;
		box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5); padding: 6px; display: flex; flex-direction: column;
	}
	.usermenu a, .demoswitch {
		display: flex; align-items: center; gap: 8px; text-align: left; background: transparent;
		border: 0; color: var(--ink-dim); font-size: 13px; padding: 9px 11px; border-radius: 8px; cursor: pointer;
	}
	.usermenu a:hover, .demoswitch:hover { background: var(--card); color: var(--ink); }
	.mdiv { height: 1px; background: var(--line); margin: 5px 2px; }
	.demotag {
		font-size: 9px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--amber);
		border: 1px solid color-mix(in srgb, var(--amber) 35%, transparent); border-radius: 5px; padding: 1px 6px;
	}

	.overlay {
		position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); backdrop-filter: blur(4px);
		z-index: 100; display: flex; align-items: flex-start; justify-content: center; padding-top: 90px;
		animation: pagein 0.2s;
	}
	.switcher {
		width: 440px; max-height: 70vh; display: flex; flex-direction: column;
		background: var(--card-2); border: 1px solid var(--line); border-radius: 16px; overflow: hidden;
		box-shadow: 0 30px 90px rgba(0, 0, 0, 0.6);
	}
	.shead { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--line); }
	.shead b { font-size: 14px; }
	.shead .x { margin-left: auto; background: transparent; border: 0; color: var(--ink-faint); cursor: pointer; font-size: 14px; }
	.ulist { overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 4px; }
	.urow {
		display: flex; align-items: center; gap: 11px; text-align: left; background: transparent;
		border: 1px solid transparent; border-radius: 10px; padding: 9px 11px; cursor: pointer;
	}
	.urow:hover { background: var(--card); }
	.urow.cur { border-color: color-mix(in srgb, var(--green) 40%, transparent); background: rgba(62, 207, 142, 0.06); }
	.urinfo { display: flex; flex-direction: column; line-height: 1.3; flex: 1; min-width: 0; }
	.urinfo b { font-size: 13px; color: var(--ink); }
	.urinfo small { font-size: 10.5px; color: var(--ink-faint); }
	.curtag { font-size: 9px; color: var(--green); }
</style>
