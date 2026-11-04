<script lang="ts">
	// Try a request (WEB_UI §5.9): same agent loop and policy path, `via: web`. Test-as is
	// admin-only and only for child or guest profiles; dry-run is on by default.
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import Chip from '#lib/components/Chip.svelte';
	import Tabs from '#lib/components/Tabs.svelte';

	type Line = { you: string; reply?: Schemas['ChatReply']; error?: string };
	let lines = $state<Line[]>([]);
	let text = $state('');
	let as = $state('');
	let dryRun = $state(true);
	let busy = $state(false);
	let people = $state<Schemas['ProfileView'][]>([]);

	$effect(() => {
		if (session.isAdmin)
			void api
				.GET('/api/profiles')
				.then(({ data }) => (people = (data ?? []).filter((p) => p.kind === 'child' || p.kind === 'guest')));
	});

	async function send(e: SubmitEvent) {
		e.preventDefault();
		if (!text.trim()) return;
		const said = text;
		text = '';
		busy = true;
		const { data, error, response } = await api.POST('/api/chat', {
			body: { text: said, as_profile_id: as || null, dry_run: dryRun, device: 'main' }
		});
		busy = false;
		const line: Line =
			response.ok && data
				? { you: said, reply: data }
				: { you: said, error: errorText(toProblem(error, response).code) };
		lines = [...lines, line];
	}
</script>

<h1>{t('nav.activity')}</h1>
<Tabs
	label={t('nav.activity')}
	items={[
		{ href: '/activity', text: t('activity.timeline') },
		{ href: '/activity/try', text: t('activity.try') }
	]}
/>
<div class="row">
	{#if session.isAdmin}
		<label class="field grow">
			<span class="label">{t('try.as')}</span>
			<select bind:value={as}>
				<option value="">{session.me?.name}</option>
				{#each people as p (p.id)}<option value={p.id}>{p.name} ({p.kind})</option>{/each}
			</select>
		</label>
	{/if}
	<label class="row"><input type="checkbox" bind:checked={dryRun} /> {t('try.dry_run')}</label>
</div>

<ol class="plain chat" aria-live="polite">
	{#each lines as line, i (i)}
		<li>
			<div><strong>{t('try.you')} ›</strong> <span dir="auto">{line.you}</span></div>
			<div>
				<strong>beepboop ›</strong>
				{#if line.error}<span class="error-text">{line.error}</span>
				{:else if line.reply}
					{#each line.reply.tool_calls as c, j (j)}
						<div class="row">
							<Chip value={c.decision} />
							<span class="mono">{c.tool}</span>
							<Chip kind="risk" value={c.risk} />
							{#if c.reason}<span class="muted">{c.reason}</span>{/if}
							{#if c.dry_run}<Chip value="dry-run" />{/if}
						</div>
					{/each}
					<span dir="auto">“{line.reply.reply}”</span>
					{#if line.reply.pending_approval_id}
						<a href="/approvals/{line.reply.pending_approval_id}">{t('try.approval')}</a>
					{/if}
				{/if}
			</div>
		</li>
	{/each}
</ol>

<form class="row" onsubmit={send}>
	<input class="grow" bind:value={text} placeholder={t('try.placeholder')} aria-label={t('try.placeholder')} maxlength="500" />
	<button class="primary" disabled={busy}>{t('try.send')}</button>
</form>

<style>
	.grow {
		flex: 1;
	}
	.chat li {
		display: grid;
		gap: var(--bb-space-1);
	}
</style>
