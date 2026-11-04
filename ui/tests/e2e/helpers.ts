import { createHmac } from 'node:crypto';
import { expect, type APIRequestContext, type Page } from '@playwright/test';

/** RFC 6238 TOTP (SHA-1, 6 digits, 30 s) for the seeded fake-core accounts. */
export function totp(secret: string, when = Date.now()): string {
	const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
	let bits = '';
	for (const ch of secret.replace(/[-\s=]/g, '').toUpperCase()) {
		bits += alphabet.indexOf(ch).toString(2).padStart(5, '0');
	}
	const bytes = (bits.match(/.{8}/g) ?? []).map((b) => parseInt(b, 2));
	const counter = Buffer.alloc(8);
	counter.writeBigUInt64BE(BigInt(Math.floor(when / 1000 / 30)));
	const h = createHmac('sha1', Buffer.from(bytes)).update(counter).digest();
	const off = (h[h.length - 1] ?? 0) & 0x0f;
	const v = (h.readUInt32BE(off) & 0x7fffffff) % 1_000_000;
	return String(v).padStart(6, '0');
}

export const SEED = {
	alex: { password: 'correct horse battery', secret: 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP' },
	sam: { password: 'correct horse battery', secret: 'KRSXG5CTMVRXEZLUKRSXG5CTMVRXEZLU' }
};

export async function login(page: Page, user: keyof typeof SEED): Promise<void> {
	await page.goto('/login');
	await page.getByLabel('Username', { exact: true }).fill(user);
	await page.getByLabel('Password', { exact: true }).fill(SEED[user].password);
	await page.getByRole('button', { name: 'Continue' }).click();
	await page.getByLabel('Code from your authenticator app').fill(totp(SEED[user].secret));
	await page.getByRole('button', { name: 'Sign in' }).click();
	await expect(page).toHaveURL(/\/$/);
}

/** Calls the fake core's device stand-in (same-origin, like the UI). */
export async function dev(
	request: APIRequestContext,
	baseURL: string,
	path: string,
	body?: unknown
) {
	const headers = { 'Sec-Fetch-Site': 'same-origin', Origin: baseURL };
	return body === undefined
		? request.get(`${baseURL}/api/_dev/${path}`)
		: request.post(`${baseURL}/api/_dev/${path}`, { headers, data: body });
}
