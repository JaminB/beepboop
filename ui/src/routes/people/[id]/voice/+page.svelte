<script lang="ts">
	// Voice tab (WEB_UI §5.3): consent first, then the device's own microphone records; the
	// phone is the teleprompter. No recording is ever stored (ADR 0018).
	import { invalidateAll } from '$app/navigation';
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { live } from '#lib/live.svelte.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { absolute } from '#lib/format.ts';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';

	let { data } = $props();
	const p = $derived(data.person);
	let consent = $state<Schemas['ConsentText'] | null>(null);
	let agreed = $state(false);
	let run = $state<Schemas['EnrollView'] | null>(null);
	let error = $state('');
	let confirmDelete = $state(false);
	const canEdit = $derived(session.isAdmin || (session.me?.profile_id === p.id && p.kind !== 'child'));

	$effect(() => {
		void api.GET('/api/enrollments/consent').then(({ data: c }) => (consent = c ?? null));
	});
	// Progress arrives as SSE `enroll` events; refresh the step text after each clip.
	$effect(() => {
		const ev = live.enroll;
		if (run && ev && ev.session_id === run.session_id) void refresh(run.session_id);
	});

	async function refresh(id: string) {
		const { data: v } = await api.GET('/api/enrollments/{session_id}', { params: { path: { session_id: id } } });
		if (v) run = v;
	}
	async function start() {
		error = '';
		const { data: v, error: err, response } = await api.POST('/api/enrollments', {
			body: { profile_id: p.id, consent: true, consent_version: consent?.version ?? '' }
		});
		if (!response.ok || !v) error = errorText(toProblem(err, response).code);
		else run = v;
	}
	async function redo() {
		if (!run) return;
		const { data: v } = await api.POST('/api/enrollments/{session_id}/redo', { params: { path: { session_id: run.session_id } } });
		if (v) run = v;
	}
	async function finish() {
		if (!run) return;
		const { error: err, response } = await api.POST('/api/enrollments/{session_id}/finish', {
			params: { path: { session_id: run.session_id } }
		});
		if (!response.ok) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		run = null;
		await invalidateAll();
		toast.show(t('voice.saved'));
	}
	async function cancel() {
		if (run) await api.DELETE('/api/enrollments/{session_id}', { params: { path: { session_id: run.session_id } } });
		run = null;
	}
	async function remove() {
		await api.DELETE('/api/profiles/{profile_id}/voice', { params: { path: { profile_id: p.id } } });
		await invalidateAll();
		toast.show(t('voice.deleted'));
	}
	const quality = $derived(live.enroll && run && live.enroll.session_id === run.session_id ? live.enroll : null);
</script>

{#if p.kind === 'guest'}
	<p>{t('voice.guest')}</p>
{:else if run}
	<section class="card enroll" aria-live="polite">
		<progress max={run.total} value={run.progress} aria-label={t('voice.progress')}></progress>
		{#if run.done}
			<p>{t('voice.all_done')}</p>
			<button class="primary wide" onclick={finish}>{t('voice.finish')}</button>
		{:else}
			<p>{run.prompt}</p>
			<p class="say" dir="auto">“{run.next_text}”</p>
			{#if quality}
				<p class="help">
					{quality.accepted ? t('voice.clip_ok') : t('voice.clip_redo')}
					· {t('voice.quality', { snr: Math.round(quality.clip_quality.snr), speech: Math.round(quality.clip_quality.speech_ratio * 100) })}
				</p>
			{/if}
		{/if}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<div class="row"><button onclick={redo}>{t('voice.redo')}</button><button class="link" onclick={cancel}>{t('common.cancel')}</button></div>
	</section>
{:else}
	<section class="card">
		{#if p.voice.enrolled}
			<p>{t('voice.enrolled', { clips: p.voice.n_clips, when: absolute(p.voice.updated_at ?? 0) })}</p>
		{:else}
			<p>{t('voice.not_enrolled')}</p>
		{/if}
	</section>
	{#if canEdit && consent}
		<section class="card consent">
			<h2>{t('voice.consent_title')}</h2>
			{#each consent.paragraphs as para, i (i)}<p>{para}</p>{/each}
			{#if p.kind === 'child'}<p class="help">{t('voice.child_consent')}</p>{/if}
			<label class="row"><input type="checkbox" bind:checked={agreed} /> {t('voice.agree')}</label>
			{#if error}<p class="error-text" role="alert">{error}</p>{/if}
			<button class="primary wide" disabled={!agreed} onclick={start}>{p.voice.enrolled ? t('voice.reenrol') : t('voice.start')}</button>
		</section>
	{/if}
	{#if canEdit && p.voice.enrolled}
		<button class="danger" onclick={() => (confirmDelete = true)}>{t('voice.delete', { name: p.name })}</button>
		<ConfirmDialog bind:open={confirmDelete} title={t('voice.delete', { name: p.name })} body={t('voice.delete_body')} action={t('voice.delete', { name: p.name })} onconfirm={remove} />
	{/if}
{/if}

<style>
	.enroll progress {
		width: 100%;
		height: 1rem;
	}
	.say {
		font-size: 1.6rem;
		text-align: center;
		margin: var(--bb-space-6) 0;
	}
	.consent {
		max-height: 60vh;
		overflow-y: auto;
	}
</style>
