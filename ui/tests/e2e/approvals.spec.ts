// ROADMAP v0.1 acceptance 5, against the fake core: a `critical` voice request produces a
// banner on the same profile's web session within 2 s; approving from a different
// profile's session fails; the nonce cannot be reused; after 60 s it is rejected and the
// device says so. (The 2 s here is a workstation run against the fake core, not a Pi
// measurement; the on-device number is pending on Pi.)
import { expect, test, type Page } from '@playwright/test';
import { dev, login } from './helpers';

test.describe.configure({ mode: 'serial' });

let alex: Page;
let sam: Page;
let base = '';
const cspViolations: string[] = [];

type Nonces = { __nonces: Record<string, string> };

/** Listen on the session's own SSE stream and keep the approval nonces it carries. */
async function captureNonces(page: Page): Promise<void> {
	await page.evaluate(() => {
		const w = window as unknown as Nonces;
		w.__nonces = {};
		const es = new EventSource('/api/events');
		es.addEventListener('approval', (e) => {
			const d = JSON.parse((e as MessageEvent).data) as { id: string; nonce?: string };
			if (d.nonce) w.__nonces[d.id] = d.nonce;
		});
	});
}

async function nonceOf(page: Page, id: string): Promise<string | undefined> {
	return page.evaluate((i) => (window as unknown as Nonces).__nonces[i], id);
}

async function approveRaw(page: Page, id: string, nonce: string) {
	return page.evaluate(
		async ([approvalId, n]) => {
			const r = await fetch(`/api/approvals/${approvalId}/approve`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ nonce: n })
			});
			const body = (await r.json()) as { code?: string };
			return { status: r.status, code: body.code };
		},
		[id, nonce] as const
	);
}

test.beforeAll(async ({ browser }, info) => {
	base = String(info.project.use.baseURL ?? '');
	alex = await (await browser.newContext({ baseURL: base })).newPage();
	sam = await (await browser.newContext({ baseURL: base })).newPage();
	for (const page of [alex, sam]) {
		page.on('console', (m) => {
			if (/Content Security Policy|Content-Security-Policy/i.test(m.text())) cspViolations.push(m.text());
		});
	}
	await login(alex, 'alex');
	await login(sam, 'sam');
	await captureNonces(alex);
});

test('critical voice request: banner within 2 s for the same profile, approved once', async ({
	request
}) => {
	const started = Date.now();
	const r = await dev(request, base, 'voice', { profile: 'Alex' });
	const { approval_id: id } = (await r.json()) as { approval_id: string };
	await expect(alex.getByTestId('approval-banner')).toBeVisible({ timeout: 2000 });
	expect(Date.now() - started).toBeLessThan(2000);

	// Sam (another profile, also signed in) gets no banner and cannot see or approve it.
	await expect(sam.getByTestId('approval-banner')).toHaveCount(0);
	await sam.goto(`/approvals/${id}`);
	await expect(sam.getByText("There's no approval here for you.")).toBeVisible();
	expect(await approveRaw(sam, id, 'f'.repeat(32))).toEqual({ status: 409, code: 'not_yours' });

	await alex.getByTestId('approval-banner').getByRole('link', { name: 'Open' }).click();
	await expect(alex).toHaveURL(new RegExp(`/approvals/${id}$`));
	await alex.getByTestId('approve').click();
	await expect(alex.getByTestId('approval-done')).toBeVisible();
	await expect
		.poll(async () => ((await (await dev(request, base, 'speech')).json()) as string[]).at(-1))
		.toBe('Unlocked.');

	// The nonce cannot be reused.
	const nonce = await nonceOf(alex, id);
	expect(nonce).toBeTruthy();
	expect(await approveRaw(alex, id, nonce ?? '')).toEqual({ status: 409, code: 'already_decided' });
});

test('an approval after its window (60 s, once extended to 120 s) is rejected and the device says so', async ({ request }) => {
	await alex.goto('/');
	await captureNonces(alex);
	const r = await dev(request, base, 'voice', { profile: 'Alex' });
	const { approval_id: id } = (await r.json()) as { approval_id: string };
	await expect(alex.getByTestId('approval-banner')).toBeVisible({ timeout: 2000 });
	await alex.getByTestId('approval-banner').getByRole('link', { name: 'Open' }).click();
	await expect(alex.getByTestId('approve')).toBeEnabled();
	// Opening the stream extended it once to 120 s (IDENTITY_AND_TRUST §8.2), so move past that.
	await dev(request, base, 'clock', { advance_s: 121 });
	const nonce = (await nonceOf(alex, id)) ?? 'x'.repeat(32);
	expect(await approveRaw(alex, id, nonce)).toEqual({ status: 409, code: 'expired' });
	await expect(alex.getByTestId('approval-expired')).toBeVisible();
	const said = (await (await dev(request, base, 'speech')).json()) as string[];
	expect(said.at(-1)).toMatch(/timed out/);
	await expect(alex.getByTestId('approval-banner')).toHaveCount(0);
});

test('dialogs and schema forms render under the CSP', async () => {
	await alex.goto('/me');
	await alex.getByRole('link', { name: 'Privacy' }).click();
	await alex.getByRole('button', { name: 'Delete all transcripts' }).click();
	await expect(alex.getByRole('dialog')).toBeVisible();
	await alex.getByRole('button', { name: 'Cancel' }).click();
	await expect(alex.getByRole('dialog')).toHaveCount(0);
	await alex.goto('/integrations/frontdoor');
	await expect(alex.getByLabel('Door name *')).toHaveValue('Front door');
	// The hash-mode CSP (WEB_UI §2) holds for everything these flows rendered.
	expect(cspViolations).toEqual([]);
});
