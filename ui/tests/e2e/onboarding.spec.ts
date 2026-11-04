// Claim-code onboarding (WEB_UI §5.1) against an unclaimed fake core: claim code, admin with
// password + TOTP, house, (skip LLM), home, (voice later), household, Recovery kit, Home.
import { expect, test } from '@playwright/test';
import { dev, totp } from './helpers';

test('first boot: claim the device and finish the wizard', async ({ page, request, baseURL }) => {
	await page.goto('/');
	await expect(page).toHaveURL(/\/claim$/);
	const { code } = (await (await dev(request, baseURL ?? '', 'claim')).json()) as { code: string };
	await page.getByLabel('Digit 1').fill(code);
	await page.getByRole('button', { name: 'Continue' }).click();
	await expect(page).toHaveURL(/\/onboarding$/);

	await page.getByLabel('Name', { exact: true }).fill('Morgan');
	await page.getByLabel('Username', { exact: true }).fill('morgan');
	await page.getByLabel('Password', { exact: true }).fill('a long and quiet house');
	const key = (await page.locator('.totp .mono').innerText()).replaceAll('-', '');
	await page.getByLabel('Code from the app').fill(totp(key));
	await page.getByRole('button', { name: 'Continue' }).click();

	await expect(page.getByRole('heading', { name: 'House' })).toBeVisible();
	await page.getByRole('button', { name: 'Continue' }).click();
	await expect(page.getByRole('heading', { name: 'Language model' })).toBeVisible();
	await page.getByRole('button', { name: 'Skip for now' }).click();
	await expect(page.getByRole('heading', { name: 'Your home' })).toBeVisible();
	await page.getByRole('button', { name: 'Continue' }).click();
	await page.getByRole('button', { name: 'Do this later' }).click();
	await page.getByRole('button', { name: 'Continue' }).click();

	await expect(page.getByRole('heading', { name: 'Recovery kit' })).toBeVisible();
	await expect(page.locator('ol.codes li')).toHaveCount(10);
	await page.getByLabel('I saved them').check();
	await page.getByRole('button', { name: 'Done' }).click();
	await expect(page.getByRole('heading', { name: /Good (morning|afternoon|evening), Morgan\./ })).toBeVisible();
});
