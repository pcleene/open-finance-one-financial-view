import adapter from '@sveltejs/adapter-auto';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
	// BACKEND_URL points the dev proxy at a deployed backend (e.g. the AWS ALB);
	// defaults to the local API. Set it in frontend/.env to demo against AWS.
	// API_KEY (server-side only) is injected as X-API-Key so the deployed API's
	// auth gate is satisfied without the key ever reaching the browser bundle.
	const env = loadEnv(mode, '.', '');
	const backend = env.BACKEND_URL || 'http://127.0.0.1:8010';
	const apiKey = env.API_KEY || '';
	// The mock OFP is a separate ALB listener (:8100). The "Link an institution"
	// flow opens the OFP authorize page in an iframe, so the BROWSER must reach
	// it — proxy /v1/oauth/authorize* to the mock. Derive host:8100 from BACKEND_URL
	// (or set MOCK_BACKEND_URL explicitly).
	const mockBackend = env.MOCK_BACKEND_URL || backend.replace(/(:\d+)?$/, ':8100');

	return {
		plugins: [
			sveltekit({
				compilerOptions: {
					// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
					runes: ({ filename }) =>
						filename.split(/[/\\]/).includes('node_modules') ? undefined : true
				},

				// adapter-auto only supports some environments, see https://svelte.dev/docs/kit/adapter-auto for a list.
				// If your environment is not supported, or you settled on a specific environment, switch out the adapter.
				// See https://svelte.dev/docs/kit/adapters for more information about adapters.
				adapter: adapter()
			})
		],
		server: {
			proxy: {
				// the client calls /api/* (VITE_API_URL=/api); the dev server forwards
				// to the backend (local or the AWS ALB) — same-origin, no CORS.
				'/api': {
					target: backend,
					changeOrigin: true,
					// the ALB terminates TLS with a self-signed cert; the proxy runs
					// server-side so we accept it here (the browser only sees localhost).
					secure: false,
					rewrite: (p) => p.replace(/^\/api/, ''),
					configure: (proxy: any) => {
						if (apiKey)
							proxy.on('proxyReq', (proxyReq: any) =>
								proxyReq.setHeader('x-api-key', apiKey));
					}
				},
				// OFP authorize page + its decision POST (the consent iframe) → mock OFP.
				// Forwarded as-is (the mock's form action is the root-relative
				// /v1/oauth/authorize/decision, so it must live at the same path here).
				'/v1/oauth/authorize': {
					target: mockBackend,
					changeOrigin: true,
					secure: false
				}
			}
		}
	};
});
