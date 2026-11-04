<script lang="ts">
	// System › Voice (WEB_UI §5.11, VOICE_PIPELINE §8.4): the wake word and its sensitivity
	// (read from /config/beepboop.yaml; hard rule 4), household speaker thresholds, "ask on
	// phone when unsure", false wakes this week, the speech providers.
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';

	let view = $state<Schemas['VoiceView'] | null>(null);
	let thresholds = $state<Schemas['VoiceThresholds']>({ tau_act: 0.7, margin_act: 0.1, tau_personal: 0.9, margin_personal: 0.2 });
	let ask = $state(true);
	let error = $state('');
	let busy = $state(false);

	async function load() {
		const { data } = await api.GET('/api/system/voice');
		if (!data) return;
		view = data;
		thresholds = { ...data.thresholds };
		ask = data.ask_when_unsure;
	}
	$effect(() => {
		void load();
	});

	async function save(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		const { error: err, response } = await api.PATCH('/api/system/voice', {
			body: { thresholds, ask_when_unsure: ask }
		});
		busy = false;
		if (!response.ok) {
			const p = toProblem(err, response);
			error = p.fields?.['thresholds.tau_personal'] === 'below_tau_act' ? t('voice.below_tau_act') : errorText(p.code);
			return;
		}
		toast.show(t('common.saved'));
		await load();
	}
	function pct(v: number): string {
		return v.toFixed(2);
	}
</script>

{#if view}
	<dl class="card grid">
		<dt>{t('voice.wake_word')}</dt>
		<dd>"{view.wake_word}"</dd>
		<dt>{t('voice.sensitivity')}</dt>
		<dd>{view.wake_sensitivity === null || view.wake_sensitivity === undefined ? t('voice.sensitivity_config') : pct(view.wake_sensitivity)} <span class="help">{t('voice.sensitivity_help')}</span></dd>
		<dt>{t('voice.false_wakes')}</dt>
		<dd>{t('voice.false_wakes_n', { count: view.false_wakes_7d })}</dd>
		<dt>{t('voice.speech')}</dt>
		<dd>STT {view.stt} · TTS {view.tts} · {t('voice.cloud', { state: view.cloud_speech ? t('common.on') : t('common.off') })}</dd>
	</dl>

	<form class="card" onsubmit={save}>
		<h2>{t('voice.thresholds')}</h2>
		<p class="help">{(view.similar ?? []).length ? t('voice.similar', { pairs: (view.similar ?? []).map((p) => p.join(' & ')).join(', ') }) : t('voice.distinct')}</p>
		<div class="grid2">
			<label class="field" for="tau-act"><span class="label">{t('voice.tau_act')}</span>
				<input id="tau-act" type="number" min="0.3" max="0.99" step="0.01" bind:value={thresholds.tau_act} /></label>
			<label class="field" for="margin-act"><span class="label">{t('voice.margin_act')}</span>
				<input id="margin-act" type="number" min="0" max="0.5" step="0.01" bind:value={thresholds.margin_act} /></label>
			<label class="field" for="tau-personal"><span class="label">{t('voice.tau_personal')}</span>
				<input id="tau-personal" type="number" min="0.3" max="0.99" step="0.01" bind:value={thresholds.tau_personal} aria-invalid={error ? 'true' : undefined} /></label>
			<label class="field" for="margin-personal"><span class="label">{t('voice.margin_personal')}</span>
				<input id="margin-personal" type="number" min="0" max="0.5" step="0.01" bind:value={thresholds.margin_personal} /></label>
		</div>
		<label class="check" for="ask-unsure">
			<input id="ask-unsure" type="checkbox" bind:checked={ask} />
			{t('voice.ask_when_unsure')}
		</label>
		<p class="help">{t('voice.thresholds_help')}</p>
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide" disabled={busy}>{t('common.save')}</button>
	</form>
{/if}

<style>
	.grid2 {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--bb-space-3);
	}
	.check {
		display: flex;
		gap: var(--bb-space-2);
		align-items: center;
		min-height: 24px;
		margin: var(--bb-space-3) 0;
	}
</style>
