<script lang="ts">
	import '../app.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { live } from '#lib/live.svelte.ts';
	import { t, tx } from '#lib/i18n.ts';
	import ApprovalBanner from '#lib/components/ApprovalBanner.svelte';
	import Toast from '#lib/components/Toast.svelte';

	let { children } = $props();

	const PUBLIC = ['/claim', '/login', '/invite', '/ca', '/onboarding'];
	const isPublic = $derived(PUBLIC.some((p) => page.url.pathname === p || page.url.pathname.startsWith(`${p}/`)));
	let ready = $state(false);
	let menuOpen = $state(false);

	$effect(() => {
		void boot(page.url.pathname);
	});

	async function boot(path: string) {
		if (!session.loaded) {
			const state = await api.GET('/api/onboarding/state');
			const me = await session.load();
			if (!state.data?.claimed && !path.startsWith('/claim') && !path.startsWith('/ca')) {
				await goto('/claim');
			} else if (!me && !PUBLIC.some((p) => path === p || path.startsWith(`${p}/`))) {
				await goto(`/login?next=${encodeURIComponent(path)}`);
			}
		}
		if (session.me) live.start();
		ready = true;
	}

	async function signOut() {
		await api.POST('/api/auth/logout');
		live.stop();
		session.set(null);
		await goto('/login');
	}

	const nav = $derived([
		{ href: '/', text: t('nav.home'), show: true },
		{ href: '/activity', text: t('nav.activity'), show: true },
		{ href: '/people', text: t('nav.people'), show: session.me?.kind !== 'guest' },
		{ href: '/integrations', text: t('nav.integrations'), show: session.me?.kind !== 'child' },
		{ href: '/system', text: t('nav.system'), show: session.me?.kind === 'admin' || session.me?.kind === 'member' }
	]);
	const banner = $derived(live.pending[0]?.summary ?? '');

	function current(href: string): boolean {
		return href === '/' ? page.url.pathname === '/' : page.url.pathname.startsWith(href);
	}
</script>

<div class="live-region sr-only" aria-live="polite">{banner ? t('banner.announce', { summary: banner }) : ''}</div>

{#if session.me && !isPublic}
	<header class="top">
		<a href="/system" class="dot-link" aria-label={t('device.label', { state: tx(`device.${live.device}`) })}>
			<span class="dot dot-{live.device}"></span>
		</a>
		<a href="/" class="brand">beepboop</a>
		<div class="menu">
			<button class="link" aria-expanded={menuOpen} onclick={() => (menuOpen = !menuOpen)}>{session.me.name} ▾</button>
			{#if menuOpen}
				<div class="menu-list card">
					<a href="/me" onclick={() => (menuOpen = false)}>{t('nav.me')}</a>
					<button class="link" onclick={signOut}>{t('nav.sign_out')}</button>
				</div>
			{/if}
		</div>
	</header>
	<ApprovalBanner />
	<div class="frame">
		<nav class="rail" aria-label={t('nav.label')}>
			{#each nav.filter((n) => n.show) as n (n.href)}
				<a href={n.href} aria-current={current(n.href) ? 'page' : undefined}>{n.text}</a>
			{/each}
		</nav>
		<main>
			{#if ready}{@render children()}{/if}
		</main>
	</div>
	<nav class="bottom" aria-label={t('nav.label')}>
		{#each nav.filter((n) => n.show).slice(0, 3) as n (n.href)}
			<a href={n.href} aria-current={current(n.href) ? 'page' : undefined}>{n.text}</a>
		{/each}
		<a href="/more" aria-current={current('/more') ? 'page' : undefined}>{t('nav.more')}</a>
	</nav>
{:else}
	<main class="bare">
		{#if ready}{@render children()}{/if}
	</main>
{/if}
<Toast />

<style>
	.top {
		display: flex;
		align-items: center;
		gap: var(--bb-space-3);
		padding: var(--bb-space-2) var(--bb-space-4);
		background: var(--bb-surface);
		border-bottom: 1px solid var(--bb-border);
	}
	.brand {
		font-weight: 700;
		color: var(--bb-text);
		text-decoration: none;
		flex: 1;
	}
	.dot-link {
		min-width: 2.75rem;
		min-height: 2.75rem;
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.dot {
		width: 0.9rem;
		height: 0.9rem;
		border-radius: 50%;
		background: var(--bb-status-dot);
	}
	.dot-listening,
	.dot-thinking,
	.dot-speaking {
		background: var(--bb-primary);
	}
	.dot-muted {
		background: var(--bb-danger);
	}
	.dot-pin,
	.dot-error {
		background: #d99a00;
	}
	.dot-offline {
		background: transparent;
		border: 2px solid var(--bb-status-dot);
	}
	.menu {
		position: relative;
	}
	.menu-list {
		position: absolute;
		inset-inline-end: 0;
		top: 100%;
		display: grid;
		min-width: 10rem;
		z-index: 10;
	}
	.menu-list a,
	.menu-list button {
		min-height: 2.75rem;
		display: flex;
		align-items: center;
	}
	.frame {
		display: flex;
	}
	main {
		flex: 1;
		max-width: var(--bb-max);
		margin-inline: auto;
		padding: var(--bb-space-4) var(--bb-space-4) 6rem;
		width: 100%;
	}
	.rail {
		display: none;
	}
	.bottom {
		position: fixed;
		bottom: 0;
		inset-inline: 0;
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		background: var(--bb-surface);
		border-top: 1px solid var(--bb-border);
	}
	.bottom a,
	.rail a {
		min-height: 3.25rem;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--bb-muted);
		text-decoration: none;
	}
	.bottom a[aria-current='page'],
	.rail a[aria-current='page'] {
		color: var(--bb-primary);
		font-weight: 600;
	}
	@media (min-width: 900px) {
		.rail {
			display: grid;
			align-content: start;
			width: 12rem;
			padding: var(--bb-space-4);
		}
		.rail a {
			justify-content: flex-start;
			padding-inline: var(--bb-space-3);
		}
		.bottom {
			display: none;
		}
	}
</style>
