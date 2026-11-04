import { defineConfig, devices } from '@playwright/test';

// The e2e suite runs the built SPA against the fake core (`beepboop.api.devserver`): the
// real /api, policy and approvals over SQLite in a temp dir, plus a /api/_dev router that
// plays the device. Build first: `pnpm build && pnpm test:e2e`. Set UV if `uv` is not on PATH.
const uv = process.env.UV ?? 'uv';
const core = (port: number, extra: string) =>
	`${uv} run --project .. python -m beepboop.api.devserver --spa build --port ${port} ${extra}`;

export default defineConfig({
	testDir: 'tests/e2e',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	reporter: [['list']],
	use: { trace: 'retain-on-failure', ...devices['Pixel 7'] },
	projects: [
		{
			name: 'approvals',
			testMatch: /approvals\.spec\.ts/,
			use: { ...devices['Pixel 7'], baseURL: 'http://localhost:8787' }
		},
		{
			name: 'onboarding',
			testMatch: /onboarding\.spec\.ts/,
			use: { ...devices['Pixel 7'], baseURL: 'http://localhost:8788' }
		}
	],
	webServer: [
		{
			command: core(8787, '--seed'),
			url: 'http://localhost:8787/api/health',
			reuseExistingServer: false,
			timeout: 60_000
		},
		{
			command: core(8788, ''),
			url: 'http://localhost:8788/api/health',
			reuseExistingServer: false,
			timeout: 60_000
		}
	]
});
