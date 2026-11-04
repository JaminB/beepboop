<script lang="ts">
	// The approve page (WEB_UI §5.7): one tap, no dialog. The primary button names the effect;
	// the nonce comes from the SSE `approval` event, never from a URL.
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { live } from '#lib/live.svelte.ts';
	import { effectLabel } from '#lib/approvals.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { relativeTime, secondsLeft } from '#lib/format.ts';

	const id = page.params.id ?? '';
	let view = $state<Schemas['ApprovalView'] | null>(null);
	let missing = $state(false);
	let outcome = $state<string | null>(null);
	let error = $state('');
	let busy = $state(false);

	$effect(() => {
		void load();
	});

	async function load() {
		const { data, response } = await api.GET('/api/approvals/{approval_id}', { params: { path: { approval_id: id } } });
		if (!response.ok || !data) missing = true;
		else view = data;
	}

	const liveA = $derived(live.get(id));
	const left = $derived(view ? secondsLeft(liveA?.exp ?? view.exp, live.now) : 0);
	const status = $derived(outcome ?? liveA?.state ?? view?.state ?? 'pending');
	const expired = $derived(status === 'expired' || (status === 'pending' && left === 0));
	const nonce = $derived(liveA?.nonce);

	async function decide(kind: 'approve' | 'deny' | 'not-me') {
		if (!nonce) return;
		busy = true;
		const path = { params: { path: { approval_id: id } }, body: { nonce } };
		const res =
			kind === 'approve'
				? await api.POST('/api/approvals/{approval_id}/approve', path)
				: kind === 'deny'
					? await api.POST('/api/approvals/{approval_id}/deny', path)
					: await api.POST('/api/approvals/{approval_id}/not-me', path);
		busy = false;
		if (res.response.ok && res.data) {
			outcome = res.data.state;
			live.settle(id, res.data.state);
		} else {
			const p = toProblem(res.error, res.response);
			error = errorText(p.code);
			if (p.code === 'expired') outcome = 'expired';
		}
	}
</script>

{#if missing}
	<div class="card"><p>{t('approve.not_found')}</p><a class="button" href="/">{t('common.back')}</a></div>
{:else if view}
	<h1>{view.summary}?</h1>
	{#if expired}
		<div class="card" data-testid="approval-expired">
			<p>{t('approve.expired')}</p>
			<button onclick={() => goto('/')}>{t('common.back')}</button>
		</div>
	{:else if status === 'approved'}
		<div class="card" data-testid="approval-done"><p>{t('approve.approved', { factor: view.factor ?? liveA?.factor ?? 'web' })}</p></div>
	{:else if status === 'denied'}
		<div class="card" data-testid="approval-denied"><p>{t('approve.denied')}</p></div>
	{:else}
		<p class="muted">
			{t(view.via === 'voice' ? 'approve.asked_voice' : 'approve.asked', { when: relativeTime(view.created_at, live.now) })}
			{#if view.confidence !== null && view.confidence !== undefined} · {t('approve.sounded', { pct: Math.round(view.confidence * 100) })}{/if}
		</p>
		<p aria-live="polite">⏱ {t('approve.left', { s: left })}</p>
		<meter min="0" max="120" value={left} class="time"></meter>
		<details>
			<summary>{t('common.details')}</summary>
			<p class="mono">{t('approve.tool')} {view.tool}<br />{t('approve.turn')} {view.turn_id ?? '—'} · {view.device ?? '—'}</p>
		</details>
		{#if !nonce}<p class="help">{t('approve.waiting_stream')}</p>{/if}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<div class="actions stack">
			<button class="primary wide" data-testid="approve" disabled={busy || !nonce} onclick={() => decide('approve')}>{effectLabel(view.summary)}</button>
			<button class="wide" disabled={busy || !nonce} onclick={() => decide('deny')}>{t('approve.dont')}</button>
			<button class="link" disabled={busy || !nonce} onclick={() => decide('not-me')}>{t('approve.not_me')}</button>
		</div>
	{/if}
{/if}

<style>
	.time {
		width: 100%;
	}
	.actions {
		margin-top: var(--bb-space-6);
	}
</style>
