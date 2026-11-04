<script lang="ts">
	// "Why did you do that?" (WEB_UI §5.8): Who, What I did, Timing, Details, in that order.
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { t } from '#lib/i18n.ts';
	import { absolute, speakerChip } from '#lib/format.ts';
	import Chip from '#lib/components/Chip.svelte';
	import Waterfall from '#lib/components/Waterfall.svelte';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';

	const id = page.params.turn_id ?? '';
	let turn = $state<Schemas['TurnDetail'] | null>(null);
	let missing = $state(false);
	let confirmDelete = $state(false);

	$effect(() => {
		void api
			.GET('/api/turns/{turn_id}', { params: { path: { turn_id: id } } })
			.then(({ data, response }) => {
				if (!response.ok || !data) missing = true;
				else turn = data;
			});
	});

	const mine = $derived(
		turn !== null && (session.isAdmin || turn.profile_id === session.me?.profile_id)
	);

	function decisionText(call: Schemas['ToolCallView']): string {
		if (call.decision === 'allow') return t('activity.allowed', { grant: call.grant_id ?? '—' });
		if (call.decision === 'deny') return t('activity.denied', { reason: call.deny_reason ?? '—' });
		return t('activity.confirm');
	}

	async function notMe() {
		const { data } = await api.POST('/api/turns/{turn_id}/not-me', {
			params: { path: { turn_id: id } },
			body: {}
		});
		if (data)
			toast.show(
				data.did.length
					? t('activity.not_me_did', { did: data.did.join('; ') })
					: t('activity.not_me_done')
			);
	}
	async function falseWake() {
		await api.POST('/api/turns/{turn_id}/false-wake', { params: { path: { turn_id: id } } });
		toast.show(t('activity.false_wake_done'));
		if (turn) turn = { ...turn, transcript: null, reply: null, false_wake: true };
	}
	async function remove() {
		await api.DELETE('/api/turns/{turn_id}', { params: { path: { turn_id: id } } });
		toast.show(t('activity.deleted'));
		await goto('/activity');
	}
</script>

{#if missing}
	<p>{t('activity.not_found')}</p>
{:else if turn}
	<h1>{absolute(turn.created_at)} · {turn.profile_name}</h1>
	{#if turn.transcript}<p class="quote" dir="auto">“{turn.transcript}”</p>{/if}
	{#if turn.reply}<p class="muted" dir="auto">↳ “{turn.reply}”</p>{/if}
	{#if turn.transcript_hidden}<p class="help">{t('activity.hidden_why')}</p>{/if}

	<h2>{t('activity.who_heading')}</h2>
	<div class="spread">
		<span>{speakerChip(turn.profile_name, turn.confidence)}</span>
		{#if mine && turn.via === 'voice'}<button class="link" onclick={notMe}>{t('approve.not_me')}</button>{/if}
	</div>
	<details>
		<summary>{t('common.details')}</summary>
		<p class="mono">
			{t('activity.who_details', {
				score: turn.confidence ?? '—',
				margin: turn.margin ?? '—',
				stage: turn.id_stage ?? '—'
			})}
		</p>
	</details>

	<h2>{t('activity.what')}</h2>
	<p>{turn.path === 'fast' ? t('activity.fast') : turn.path === 'llm' ? t('activity.llm') : turn.path}</p>
	<ul class="plain">
		{#each turn.tool_calls as call (call.seq)}
			<li>
				<div class="spread"><span class="mono">{call.tool}</span><Chip kind="risk" value={call.effective_risk} /></div>
				<div class="mono small">{JSON.stringify(call.args)}</div>
				<div class="small">
					{decisionText(call)}
					{#if call.factor} · {t('activity.factor', { factor: call.factor })}{/if}
					{#if call.untrusted_output} · ⚠ {t('activity.untrusted')}{/if}
					· {call.result_status}
				</div>
			</li>
		{/each}
	</ul>

	<h2>{t('activity.timing')}</h2>
	<Waterfall stages={turn.stages_ms} />

	<details>
		<summary>{t('common.details')}</summary>
		<p class="mono small">
			{t('activity.details_line', {
				tools: turn.tools_offered,
				tin: turn.tokens_in,
				tout: turn.tokens_out,
				cost: (turn.cost_micro_usd / 1e6).toFixed(4),
				provider: turn.llm_provider ?? 'none',
				locale: turn.locale
			})}
			<br />{turn.id}
		</p>
	</details>

	{#if mine}
		<div class="row actions">
			{#if turn.via === 'voice' && !turn.false_wake}<button onclick={falseWake}>{t('activity.false_wake')}</button>{/if}
			<button class="danger" onclick={() => (confirmDelete = true)}>{t('activity.delete')}</button>
		</div>
		<ConfirmDialog
			bind:open={confirmDelete}
			title={t('activity.delete')}
			body={t('activity.delete_body')}
			action={t('activity.delete')}
			onconfirm={remove}
		/>
	{/if}
{/if}

<style>
	.quote {
		font-size: 1.2rem;
	}
	.small {
		font-size: 0.85rem;
	}
	.actions {
		margin-top: var(--bb-space-6);
	}
</style>
