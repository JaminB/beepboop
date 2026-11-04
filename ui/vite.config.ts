import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [
		sveltekit({
			// A pure SPA served by FastAPI (WEB_UI §2): every route falls back to index.html.
			adapter: adapter({ pages: 'build', assets: 'build', fallback: 'index.html', strict: true }),
			// CSP in hash mode (WEB_UI §2): written as a <meta> into the static shell.
			csp: {
				mode: 'hash',
				directives: {
					'default-src': ['self'],
					'img-src': ['self', 'data:'],
					'connect-src': ['self'],
					'script-src': ['self'],
					// SvelteKit's route announcer is the one inline style attribute; allow exactly it.
					'style-src': ['self', 'unsafe-hashes', 'sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo='],
					'object-src': ['none'],
					'base-uri': ['self'],
					'form-action': ['self']
				}
			}
		})
	],
	server: {
		// `pnpm dev` against a running fake core: `pnpm fake-core --seed --port 8787`.
		proxy: { '/api': { target: 'http://localhost:8787' } }
	},
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node'
	}
});
