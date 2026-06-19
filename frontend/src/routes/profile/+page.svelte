<script lang="ts">
	import { onMount } from 'svelte';
	import { api, API, MOCK, cid, fmtDate, INSTITUTION_COLORS, SHORT_NAMES } from '$lib/api';
	import HowItWorks from '$lib/HowItWorks.svelte';

	let showHow = $state(false);

	let data: any = $state(null);
	let me: any = $state(null);
	let busy = $state('');
	let modal: { url: string } | null = $state(null);
	let toast = $state('');
	let linkForm = $state({
		dp_id: 'DP-BANKB-001-7F3A',
		consent_purpose: 'pfm',
		months: 6,
		perms: { read_accounts: true, read_balances: true, read_transactions: true }
	});

	// "How it works" — two flows (tabs): linking a consent and revoking one. Each
	// step carries the API endpoint, the consent effect, the code + the MongoDB query.
	const KAFKA_PUBLISH_CODE = `# consent/producer.py \u00b7 publish_consent_event()  (transport = "kafka")
doc["_rcp_version"] = int(updated_at.timestamp() * 1000)   # monotonic per consent_id
await get_consent_producer().publish(
    topic="rcp.consent.events",   # Acme's consent topic
    key=doc["_id"],               # = consent_id \u2192 per-consent ordering on its partition
    value=doc)                    # Extended JSON (bson.json_util) \u2192 keeps Date / Decimal128
# \u2192 the MongoDB Kafka Connect sink consumes the topic and upserts acme consents`;
	const SINK_UPSERT_QUERY = `// applied by the MongoDB Kafka Connect sink (ops/kafka/consents-mongo-sink.json):
db.consents.replaceOne(
  { _id: consent_id },   // ProvidedInValueStrategy \u2192 _id taken from the event value
  doc,                   // ReplaceOneDefaultStrategy
  { upsert: true })      // per-consent partition ordering \u2192 latest event wins`;
	const WATCHER_PROJECT = `# eraser/worker.py \u00b7 consents_watcher()
async with await db.consents.watch(                       # tail the consents collection
        [{"$match": {"operationType": {"$in": ["insert", "replace", "update"]}}}],
        resume_after=token) as stream:                    # resumable from stream_checkpoints
    async for change in stream:
        consent = change["fullDocument"]
        if consent["status"] == "authorized":
            await project_boxes_for_consent(consent)      # boxes onto embedded accounts
            asyncio.create_task(run_backfill(consent))    # \u2192 backfill (step 6)
        await save_token("consents_watch", change["_id"]) # checkpoint`;
	const WATCHER_GATEFLIP = `# eraser/worker.py \u00b7 consents_watcher() + gate_flip()
async with await db.consents.watch(                       # the change-stream listener
        [{"$match": {"operationType": {"$in": ["insert", "replace", "update"]}}}],
        resume_after=token) as stream:
    async for change in stream:
        consent = change["fullDocument"]
        if consent["status"] in ("revoked", "suspended", "expired"):
            async with await sess.start_transaction():     # ACID: flip + audit, atomically
                await db.customer_profiles.update_one(...)  # boxes \u2192 revoked
                await db.consent_audit_log.insert_one({"action": "gate_flip", ...})
            if consent["status"] == "revoked":
                asyncio.create_task(erase_consent_data(consent))   # \u2192 step 4
        await save_token("consents_watch", change["_id"])`;

	const CONNECT_FLOW = [
		{
			title: 'Push the authorization request (PAR)',
			tone: 'api',
			api: { method: 'POST', path: '/consents/{customer_id}/link  →  OFP /v1/oauth/par' },
			desc: 'Acme records a link state and pushes an authorization request to the OFP — client_id, redirect_uri, the customer\u2019s hashed id (login_hint) and the authorization_details (DP, purpose, permissions, EOD expiry). The OFP returns a request_uri.',
			affects: 'A pending consent is minted at the OFP in status awaiting_authorization — no data access yet.',
			file: 'services/consent_service.py \u00b7 link()',
			code: `r = await hc.post("/v1/oauth/par", json={
    "client_id": dc_id, "redirect_uri": f"{redirect_base}/consents-callback",
    "login_hint": { "hashed_id_number": ... },
    "authorization_details": { "dp_id": dp_id, "consent_purpose": purpose,
        "permissions": permissions, "expiration_datetime": eod }})
request_uri = r.json()["request_uri"]`,
			query: 'db.dc_link_states.insert_one({ _id: state, customer_id, dp_id, consent_purpose })'
		},
		{
			title: 'Authorize — the data provider\u2019s account picker',
			tone: 'api',
			api: { method: 'GET', path: '/v1/oauth/authorize?request_uri=\u2026 \u00b7 POST /v1/oauth/authorize/decision' },
			desc: 'The modal is the DP\u2019s own consent screen (not Acme\u2019s UI): the customer ticks which accounts to share and taps Allow. On approve the OFP attaches the chosen accounts and issues a one-time auth code.',
			affects: 'Consent transitions awaiting_authorization \u2192 authorized at the OFP, scoped to the selected accounts.',
			file: 'mock_ofp/app.py \u00b7 authorize_decision()',
			code: `# on decision == "approve"
await db.consents.update_one({"consent_id": cid},
    {"$set": {"accounts": consented}})
consent = await transition(consent, "authorized", ...)   # awaiting \u2192 authorized
code = secrets.token_urlsafe(24)                          # one-time auth code`,
			query: 'db.consents.update_one({ consent_id }, { $set: { accounts: consented } })'
		},
		{
			title: 'Callback + token exchange',
			tone: 'api',
			api: { method: 'GET', path: '/consents-callback?code&state  →  OFP /v1/oauth/token' },
			desc: 'The browser is redirected back to Acme with the code; Acme exchanges it for an access/refresh token (authorization_code grant) and stores it for the pulls, then hands the authoritative post-image to the one ordered path.',
			affects: 'Acme now holds the access token bound to this authorized consent.',
			file: 'services/consent_service.py \u00b7 link_callback()',
			code: `tok = (await hc.post("/v1/oauth/token", json={
    "grant_type": "authorization_code", "code": code })).json()
consent = tok["authorization_details"][0]
await publish_consent_event(db, consent)                 # \u2192 step 4`,
			query: 'db.dc_tokens.replace_one({ consent_id }, { access_token, refresh_token, customer_id }, { upsert: true })'
		},
		{
			title: 'Publish the consent event to the Kafka topic',
			tone: 'kafka',
			desc: 'The consent post-image is PRODUCED to the Acme consent topic, keyed by consent_id so every event for one consent stays ordered on its partition (monotonic _rcp_version). The MongoDB Kafka Connect sink consumes the topic and upserts the consents collection.',
			affects: 'acme consents flips to authorized when the sink applies the event; per-consent partition ordering (key = consent_id) guarantees latest-wins.',
			file: 'consent/producer.py \u00b7 publish_consent_event()',
			code: KAFKA_PUBLISH_CODE,
			query: SINK_UPSERT_QUERY
		},
		{
			title: 'Change stream watcher projects the consent boxes',
			tone: 'event',
			desc: 'The worker tails the consents change stream. On authorized it projects the consent \u201cboxes\u201d onto the matching embedded accounts in the customer profile, then schedules the backfill. Reads can enforce the new consent the moment the boxes land.',
			affects: 'The profile\u2019s embedded accounts gain the authorized box (status \u00b7 purpose \u00b7 permissions \u00b7 EOD expiry) — the live gate every read checks.',
			file: 'eraser/worker.py \u00b7 handle_consent_event() → project_boxes_for_consent()',
			code: WATCHER_PROJECT,
			query: `db.customer_profiles.update_one(
  { _id: customer_id, "accounts.account_id": account_id },
  { $set: { "accounts.$.consents": boxes } })`
		},
		{
			title: 'Backfill — pull, enrich, consolidate',
			tone: 'mongo',
			api: { method: 'GET', path: '/v1/accounts/{id} \u00b7 /balances \u00b7 /transactions  (OFP, per permission)' },
			desc: 'The worker pulls from the OFP per granted permission: the account object, the point-in-time balance, and the cursor-paginated transaction history. Each row is enriched at write time and idempotently upserted; the 90-day EOD balance series is reconstructed, the profile summary refreshed, and recurring payments detected.',
			affects: 'Consented accounts + transactions are now consolidated — One View, Transactions/Search and Insights light up.',
			file: 'ingestion/backfill.py \u00b7 backfill_consent() + pull_transactions()',
			code: `for t in page:                                     # cursor-paginated, newest-first
    doc = { "_id": "dp_id::transaction_id", **t,   # raw DP fields kept verbatim
            "amount": Decimal128(...),             # exact money
            "enrichment": categorize(t) }          # category / merchant at write time
    ops.append(UpdateOne({ "_id": doc["_id"] }, { "$set": doc }, upsert=True))`,
			query: `db.transactions.bulk_write(ops, ordered=False)   // idempotent upserts
db.balance_snapshots.insert_one({ as_of, meta, current_balance })
// + reconstruct 90-day EOD series \u00b7 refresh_summary \u00b7 detect_recurring`,
			note: 'Permission-scoped (no read_transactions \u2192 no rows). Idempotent: _id = dp_id::transaction_id. uw_features is NOT built here — it\u2019s computed on-demand at a loan inquiry.'
		}
	];

	const REVOKE_FLOW = [
		{
			title: 'Revoke via the Consent LCM',
			tone: 'api',
			api: { method: 'POST', path: '/consents/action/{consent_id}/revoke  →  OFP /v1/consents/{id}/revoke' },
			desc: 'The customer taps Revoke. Acme calls the OFP Consent Lifecycle endpoint (client-credentials grant). The OFP returns the authoritative post-image with status revoked.',
			affects: 'Consent \u2192 revoked at the OFP (source of truth for the state change).',
			file: 'services/consent_service.py \u00b7 action()',
			code: `r = await hc.post(f"/v1/consents/{consent_id}/revoke",
    json={"updated_by": "data_consumer_user"},
    headers={"authorization": f"Bearer {token}"})
await publish_consent_event(db, r.json())          # \u2192 step 2`
		},
		{
			title: 'Publish the consent event to the Kafka topic',
			tone: 'kafka',
			desc: 'The revoked post-image is PRODUCED to the Acme consent topic, keyed by consent_id (per-consent ordering, monotonic _rcp_version). The MongoDB Kafka Connect sink consumes it and upserts the consents collection.',
			affects: 'acme consents flips to revoked when the sink applies the event.',
			file: 'consent/producer.py \u00b7 publish_consent_event()',
			code: KAFKA_PUBLISH_CODE,
			query: SINK_UPSERT_QUERY
		},
		{
			title: 'Change stream → synchronous ACID gate-flip',
			tone: 'txn',
			desc: 'The worker tails consents; on revoked it flips the embedded boxes AND writes the audit entry inside ONE multi-document transaction, then schedules erasure. From the instant it commits, every read path excludes the accounts — no matter how many rows still await deletion.',
			affects: 'Profile boxes \u2192 revoked \u2192 One View, Insights and search are gated out immediately (single-digit ms).',
			file: 'eraser/worker.py \u00b7 consents_watcher() + gate_flip()',
			code: WATCHER_GATEFLIP,
			query: `db.customer_profiles.update_one({ _id: customer_id },
  { $set: { "accounts.$[a].consents.$[c].status": "revoked" } },
  arrayFilters: [ { "a.consents.consent_id": cid }, { "c.consent_id": cid } ])`
		},
		{
			title: 'Async chunked physical erasure',
			tone: 'worker',
			desc: 'Deletion runs after the flip, only for accounts no longer covered by ANY other authorized consent (set difference). Transactions are deleted in 2,000-_id batches off the covering index — no scans, no locks — with a heartbeat into the erasure_job.',
			affects: 'Transactions, balance snapshots and the embedded account are physically erased for the revoked accounts; the summary is refreshed.',
			file: 'eraser/worker.py \u00b7 erase_consent_data()',
			code: `while ids := db.transactions.find(                  # 2,000-_id batches
        { customer_id, "account.account_id": { $in: erase_set } },
        { _id: 1 }).limit(2000):
    db.transactions.delete_many({ _id: { $in: ids } })
    db.erasure_jobs.update_one({ _id: job }, { $currentDate: { updated_at: true } })`,
			query: `db.balance_snapshots.delete_many({ "meta.account_id": { $in: erase_set } })
db.uw_features.update_one({ _id: cid }, { $pull: { accounts: { account_id: { $in: erase_set } } } })
db.customer_profiles.update_one({ _id: cid }, { $pull: { accounts: { account_id: { $in: erase_set } } } })`
		},
		{
			title: 'Self-healing retry — the erasure sweeper',
			tone: 'event',
			tag: 'Self-heal',
			desc: 'Erasure is fire-and-forget off the change stream, so a worker crash mid-delete would orphan rows. A reconciliation sweeper re-drives any job left running (stale heartbeat) or failed, idempotently — the retry guarantee, without the cost/limits of wrapping a bulk delete in a transaction.',
			affects: 'Guarantees erasure eventually completes for every revoked consent.',
			file: 'eraser/worker.py \u00b7 erasure_sweeper()',
			query: `db.erasure_jobs.find_one_and_update(
  { status: { $in: ["running","failed"] }, attempts: { $lt: 5 },
    updated_at: { $lt: now - 120s } },              // stale heartbeat = died mid-flight
  { $set: { status: "running" }, $inc: { attempts: 1 },
    $currentDate: { updated_at: true } })            // claim = lease → re-drive (idempotent)`
		}
	];

	const HOW_TABS = [
		{
			label: 'Connect via Open Finance Platform',
			subtitle: 'Linking a consent: PAR → authorize → callback → one ordered consent event → backfill. Expand any step for the API, consent effect, code + MongoDB.',
			footer: 'On-prem the consent event rides Acme\u2019s stream (RCP commit → outbox → Kafka → sink → consents); the POC default applies the same guarded upsert in-process. Seconds after Allow, the institution appears across One View, Transactions and Insights.',
			steps: CONNECT_FLOW
		},
		{
			label: 'Revoke',
			subtitle: 'Revoking a consent: LCM → ordered event → ACID gate-flip → chunked erasure → self-healing sweeper. Expand any step for the API, consent effect, code + MongoDB.',
			footer: 'Gate-flip is ACID + synchronous (reads stop now); erasure is asynchronous, chunked and idempotent (retried by the sweeper). The same pipeline the Scale Ops revocation storm drives at volume.',
			steps: REVOKE_FLOW
		}
	];

	async function refresh() {
		data = await api(`/consents/${cid()}`);
		const custs = (await api('/customers')).customers;
		me = custs.find((c: any) => c.customer_id === cid()) ?? null;
	}
	onMount(() => {
		refresh();
		const onMsg = (e: MessageEvent) => {
			if (e.data === 'consent-flow-done') {
				modal = null;
				flash('Consent event published → Kafka-path → consents → change stream → profile');
				setTimeout(refresh, 800);
				setTimeout(refresh, 3000);
			}
		};
		window.addEventListener('message', onMsg);
		return () => window.removeEventListener('message', onMsg);
	});

	function flash(msg: string) {
		toast = msg;
		setTimeout(() => (toast = ''), 5000);
	}

	async function action(consentId: string, act: string) {
		busy = consentId + act;
		try {
			await api(`/consents/action/${consentId}/${act}`, { method: 'POST' });
			flash(`${act} → post-image published · gate flips on next read${act === 'revoke' ? ' · erasure job queued' : ''}`);
			setTimeout(refresh, 700);
			setTimeout(refresh, 2500);
		} catch (e: any) {
			flash(`error: ${JSON.stringify(e.body?.detail ?? e.message)}`);
		}
		busy = '';
	}

	async function startLink() {
		// the Open Finance platform: permissions sit at consent level (apply to all accounts in the
		// consent). read_accounts is required to enumerate accounts.
		const permissions = ['read_accounts'];
		if (linkForm.perms.read_balances) permissions.push('read_balances');
		if (linkForm.perms.read_transactions) permissions.push('read_transactions');
		// Browser-facing bases for the iframe + callback. When the UI runs behind
		// the dev proxy (VITE_API_URL='/api'), the OFP authorize page is reachable
		// same-origin at /v1/oauth/authorize (proxied to the mock) and the callback
		// at /api/consents-callback; fully-local falls back to the absolute hosts.
		const proxied = API.startsWith('/');
		const d = await api(`/consents/${cid()}/link`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({
				dp_id: linkForm.dp_id,
				consent_purpose: linkForm.consent_purpose,
				permissions,
				validity_days: linkForm.months * 30,
				authorize_base: proxied ? '' : MOCK,
				redirect_base: API
			})
		});
		modal = { url: d.authorize_url };
	}

	function daysLeft(iso: string) {
		return Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000);
	}
	const statusClass: Record<string, string> = {
		authorized: 'ok', suspended: 'warn', revoked: 'bad', expired: 'bad',
		rejected: 'bad', failed: 'bad', awaiting_authorization: 'warn'
	};
	const revokedCount = $derived(
		(data?.history ?? []).filter((h: any) => h.status === 'revoked').length
	);
</script>

<div class="page">
	{#if toast}<div class="toast mono">{toast}</div>{/if}

	<!-- user header -->
	{#if me}
		<div class="profilehead card">
			<span class="bigavatar">{me.preferred_name.charAt(0)}</span>
			<div class="phinfo">
				<div class="phname">{me.full_name}</div>
				<div class="phseg mono">{me.segment} segment · {me.customer_id}</div>
			</div>
			<div class="phstats">
				<div class="phstat"><b>{me.external_account_count}</b><span>accounts tracked</span></div>
				<div class="phstat"><b>{me.active_consent_count}</b><span>active consents</span></div>
				<div class="phstat"><b>{me.institutions?.length ?? 0}</b><span>institutions</span></div>
				<div class="phstat"><b class:dim={!revokedCount}>{revokedCount}</b><span>revoked</span></div>
			</div>
		</div>
	{/if}

	<div class="grid" style="grid-template-columns: 1.6fr 1fr; align-items: start">
		<section>
			<h3 class="sectit">
				<span>Linked institutions &amp; consents</span>
				<button class="howbtn" onclick={() => (showHow = true)}>How it works</button>
			</h3>
			{#if data}
				{#each data.current as c (c.consent_id)}
					<div class="consent" style="--brand: {INSTITUTION_COLORS[c.dp_id] ?? '#888'}">
						<div class="chead">
							<span class="swatch"></span>
							<b>{SHORT_NAMES[c.dp_id] ?? c.institution.name}</b>
							<span class="chip {statusClass[c.status]}"><i></i>{c.status}</span>
							<span class="purpose mono">{c.consent_purpose}</span>
							<span class="cid mono">{c.consent_id}</span>
						</div>
						<div class="cbody">
							<div class="perms">
								{#each c.permissions as p}<span class="permchip mono">{p}</span>{/each}
							</div>
							<div class="expiry mono">
								{#if c.status === 'authorized'}
									{@const dl = daysLeft(c.expiration_datetime)}
									<span class="exp" class:soon={dl <= 14}>expires in {dl} days</span>
									· {fmtDate(c.expiration_datetime)} (EOD 23:59:59 +08:00)
								{:else if c.status_reason}
									{c.status_reason.reason_code}: {c.status_reason.reason_description ?? ''}
								{/if}
							</div>
							<div class="accounts">
								{#each c.accounts ?? [] as a}
									<span class="acctpill mono">{a.account_name} · {a.account_number}</span>
								{/each}
							</div>
						</div>
						<div class="cactions">
							{#if c.status === 'authorized'}
								<button class="btn" disabled={busy !== ''} onclick={() => action(c.consent_id, 'suspend')}>Suspend</button>
								<button class="btn ghost-danger" disabled={busy !== ''} onclick={() => action(c.consent_id, 'revoke')}>
									{busy === c.consent_id + 'revoke' ? 'Revoking…' : 'Revoke'}
								</button>
							{:else if c.status === 'suspended'}
								<button class="btn" disabled={busy !== ''} onclick={() => action(c.consent_id, 'reactivate')}>Reactivate</button>
								<button class="btn ghost-danger" disabled={busy !== ''} onclick={() => action(c.consent_id, 'revoke')}>Revoke</button>
							{/if}
						</div>
					</div>
				{/each}
				{#if !data.current.length}
					<div class="card">No consents yet — link an institution.</div>
				{/if}
			{:else}
				<div class="skeleton" style="height: 300px"></div>
			{/if}
		</section>

		<section class="card sticky">
			<h3>Link an institution</h3>
			<p class="hint">
				Runs the real PAR → authorize flow against the OFP: the modal is the
				<b>data provider's</b> account picker, not Acme's UI.
			</p>
			<label>Institution
				<select bind:value={linkForm.dp_id}>
					{#if data}
						{#each data.providers as p}
							<option value={p.dp_id}>{p.name}{p.provider_type === 'pension_fund' ? ' · pension fund' : ''}</option>
						{/each}
					{/if}
				</select>
			</label>
			<label>Open Finance purpose
				<select bind:value={linkForm.consent_purpose}>
					<option value="pfm">pfm (powers One View + Insights)</option>
					<option value="credit_underwriting">credit_underwriting</option>
				</select>
			</label>
			<label>Permissions <small class="lvl">consent-level · apply to all accounts</small>
				<div class="permpick">
					<label class="pcheck disabled"><input type="checkbox" checked disabled /> read_accounts</label>
					<label class="pcheck"><input type="checkbox" bind:checked={linkForm.perms.read_balances} /> read_balances</label>
					<label class="pcheck"><input type="checkbox" bind:checked={linkForm.perms.read_transactions} /> read_transactions</label>
				</div>
			</label>
			<label>Validity
				<select bind:value={linkForm.months}>
					<option value={3}>3 months</option>
					<option value={6}>6 months</option>
					<option value={12}>12 months</option>
				</select>
			</label>
			<button class="btn primary" style="width:100%; margin-top: 14px" onclick={startLink}>
				Connect via Open Finance Platform →
			</button>
			<div class="renew mono">
				Renewal protocol: a new consent for the same (user, DP, purpose) auto-revokes
				its predecessor with reason_code=duplicate.
			</div>

			{#if data?.history?.length}
				<h3 style="margin-top: 26px">History</h3>
				{#each data.history.slice(0, 8) as h}
					<div class="hist mono">
						<span class="chip {statusClass[h.status]}">{h.status}</span>
						{SHORT_NAMES[h.dp_id]} · {h.consent_purpose}
						{#if h.status_reason}· {h.status_reason.reason_code}{/if}
					</div>
				{/each}
			{/if}
		</section>
	</div>
</div>

{#if modal}
	<div class="overlay" onclick={() => (modal = null)} role="presentation">
		<div class="sheet" onclick={(e) => e.stopPropagation()} role="dialog" tabindex="-1">
			<iframe src={modal.url} title="DP authorization"></iframe>
		</div>
	</div>
{/if}

{#if showHow}
	<HowItWorks
		title="Consent lifecycle — how it works"
		tabs={HOW_TABS}
		onclose={() => (showHow = false)}
	/>
{/if}

<style>
	.toast {
		position: fixed; top: 80px; right: 28px; z-index: 60;
		background: var(--card-2); border: 1px solid var(--green); color: var(--green);
		font-size: 11.5px; padding: 12px 16px; border-radius: 10px; max-width: 380px; animation: pagein 0.25s;
	}

	.profilehead { display: flex; align-items: center; gap: 18px; margin-bottom: 20px; }
	.bigavatar {
		width: 52px; height: 52px; border-radius: 50%; flex-shrink: 0;
		display: flex; align-items: center; justify-content: center;
		background: linear-gradient(135deg, var(--red), #8a0000); color: #fff; font-weight: 700; font-size: 22px;
	}
	.phinfo { flex: 1; }
	.phname { font-family: var(--serif); font-size: 24px; }
	.phseg { font-size: 11px; color: var(--ink-faint); margin-top: 2px; text-transform: capitalize; }
	.phstats { display: flex; gap: 26px; }
	.phstat { text-align: right; }
	.phstat b { display: block; font-size: 22px; color: var(--ink); }
	.phstat b.dim { color: var(--ink-faint); }
	.phstat span { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-faint); }

	.sectit { margin: 0 0 12px; font-size: 13px; color: var(--ink-dim);
		display: flex; align-items: center; justify-content: space-between; gap: 10px; }
	.howbtn { background: transparent; border: 1px solid var(--line-2); color: var(--ink-dim);
		font-family: var(--sans); font-size: 10.5px; font-weight: 600; padding: 4px 11px;
		border-radius: 999px; cursor: pointer; transition: all 0.15s; white-space: nowrap; }
	.howbtn:hover { color: var(--ink); border-color: var(--ink-faint); }
	.consent {
		background: linear-gradient(180deg, var(--card-2), var(--card));
		border: 1px solid var(--line); border-left: 3px solid var(--brand);
		border-radius: 14px; padding: 16px 18px; margin-bottom: 14px;
	}
	.chead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
	.swatch { width: 9px; height: 9px; border-radius: 3px; background: var(--brand); }
	.chip { display: inline-flex; align-items: center; gap: 5px; }
	.chip i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
	.purpose { font-size: 11px; color: var(--blue); }
	.cid { margin-left: auto; font-size: 10px; color: var(--ink-faint); }
	.cbody { margin: 12px 0; }
	.perms { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
	.permchip { font-size: 10px; color: var(--ink-dim); border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px; background: var(--card); }
	.expiry { font-size: 11.5px; color: var(--ink-dim); }
	.exp { color: var(--green); }
	.exp.soon { color: var(--amber); }
	.accounts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
	.acctpill { font-size: 10px; color: var(--ink-faint); border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px; }
	.cactions { display: flex; gap: 8px; }

	.sticky { position: sticky; top: 86px; }
	.hint { font-size: 12px; color: var(--ink-dim); line-height: 1.55; margin: 0 0 14px; }
	label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--ink-faint); margin-top: 12px; }
	label .lvl { text-transform: none; letter-spacing: 0; color: var(--ink-faint); font-size: 10px; }
	label select { display: block; width: 100%; margin-top: 5px; }
	.permpick { display: flex; flex-direction: column; gap: 6px; margin-top: 7px; }
	.pcheck { display: flex; align-items: center; gap: 8px; text-transform: none; letter-spacing: 0; font-size: 12.5px; color: var(--ink-dim); margin: 0; }
	.pcheck.disabled { color: var(--ink-faint); }
	.pcheck input { width: auto; }
	.renew { font-size: 10px; color: var(--ink-faint); line-height: 1.6; margin-top: 14px; }
	.hist { font-size: 11px; color: var(--ink-dim); padding: 6px 0; border-top: 1px solid var(--line); display: flex; gap: 8px; align-items: center; }

	.overlay {
		position: fixed; inset: 0; background: rgba(0, 0, 0, 0.65); backdrop-filter: blur(4px);
		z-index: 100; display: flex; align-items: center; justify-content: center; animation: pagein 0.2s;
	}
	.sheet {
		width: 600px; height: 640px; border-radius: 18px; overflow: hidden;
		border: 1px solid var(--line-2); box-shadow: 0 30px 90px rgba(0, 0, 0, 0.6);
	}
	.sheet iframe { width: 100%; height: 100%; border: 0; background: #f4f5f7; }
</style>
