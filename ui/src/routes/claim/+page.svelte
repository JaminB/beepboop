<script lang="ts">
	// First boot: the device spoke a six-digit code; type it here (WEB_UI §5.1 step 0).
	import { goto } from '$app/navigation';
	import { api, toProblem } from '#lib/api/client.ts';
	import { errorText, t } from '#lib/i18n.ts';

	let digits = $state(['', '', '', '', '', '']);
	let error = $state('');
	let busy = $state(false);
	let fingerprint = $state<string | null>(null);

	$effect(() => {
		void api.GET('/api/ca').then(({ data }) => (fingerprint = data?.fingerprint ?? null));
	});

	function onInput(i: number, e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		const v = el.value.replace(/\D/g, '');
		if (v.length > 1) {
			// Pasting the whole code into one box fills them all.
			v.slice(0, 6).split('').forEach((d, j) => (digits[j] = d));
		} else {
			digits[i] = v;
			if (v && i < 5) (el.parentElement?.children[i + 1] as HTMLInputElement | undefined)?.focus();
		}
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		const { data, error: err, response } = await api.POST('/api/onboarding/claim', { body: { code: digits.join('') } });
		busy = false;
		if (response.ok && data) {
			await goto(data.mode === 'first_admin' ? '/onboarding' : '/onboarding?recover=1');
			return;
		}
		error = errorText(toProblem(err, response).code);
	}
</script>

<h1>beepboop</h1>
<form class="card stack" onsubmit={submit}>
	<p>{t('claim.intro')}</p>
	<div class="digits" role="group" aria-label={t('claim.code')}>
		{#each digits as d, i (i)}
			<input value={d} inputmode="numeric" maxlength="6" aria-label={t('claim.digit', { n: i + 1 })}
				oninput={(e) => onInput(i, e)} autocomplete="one-time-code" />
		{/each}
	</div>
	<p class="help">{t('claim.didnt_hear')}</p>
	{#if fingerprint}<p class="help">{t('claim.fingerprint', { fp: fingerprint })} <a href="/ca">{t('claim.install')}</a></p>{/if}
	{#if error}<p class="error-text" role="alert">{error}</p>{/if}
	<button class="primary wide" disabled={busy || digits.join('').length !== 6}>{t('common.continue')}</button>
</form>

<style>
	.digits {
		display: grid;
		grid-template-columns: repeat(6, 1fr);
		gap: var(--bb-space-2);
	}
	.digits input {
		text-align: center;
		font-size: 1.5rem;
		padding: var(--bb-space-2) 0;
	}
</style>
