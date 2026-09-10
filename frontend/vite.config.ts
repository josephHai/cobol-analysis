import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Dev server and API proxy.
 *
 * The proxy exists for two reasons:
 *
 * 1. It avoids CORS during development by making the API same-origin.
 * 2. It **injects the identity header** the backend reads in `AUTH_MODE=trusted_header`. The
 *    browser therefore sends no credential at all — which is the point: the console has no
 *    login step, and nothing secret reaches the bundle or `sessionStorage`.
 *
 * In production the same role is played by the SSO gateway, which sets the same header. That
 * is why the header name defaults to the gateway's name rather than to something invented
 * here.
 *
 * `VITE_API_TARGET` selects the backend. It is read from `process.env` with a local
 * declaration instead of pulling in `@types/node`, which the browser bundle does not otherwise
 * need.
 */
declare const process: { env: Record<string, string | undefined> }

/**
 * Shape of the callback Vite hands to `proxy.configure`.
 *
 * Vite passes the **http-proxy instance**, whose event registration lives on the proxy object
 * itself — not on the dev server, which is what `ProxyServer` in Vite's types describes. Using
 * a local structural type avoids pulling `@types/node` into a config file that the browser
 * bundle never sees.
 */
interface ConfiguredProxy {
  on(event: 'proxyReq', listener: (proxyReq: { setHeader(name: string, value: string): void }) => void): void
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.env.INIT_CWD ?? '.', '')

  // Must match TRUSTED_ACTOR_HEADER on the server (default "E2E-token").
  const identityHeader = env.VITE_IDENTITY_HEADER ?? 'E2E-token'
  // Must be a value the server accepts. This is an identity, not a secret: it never leaves
  // this process, and the role comes from the server's TRUSTED_DEFAULT_ROLE. It is not the
  // SSO token — when that integration lands, the gateway supplies it, not this file.
  const identityValue = env.VITE_IDENTITY_VALUE ?? 'dev-proxy'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: env.VITE_API_TARGET ?? 'http://127.0.0.1:8000',
          changeOrigin: true,
          // `configure` rather than a static `headers` map: this way the header is attached to
          // every proxied request including the SSE stream, and it is unmistakable that the
          // browser is not the one supplying it.
          configure: (proxy) => {
            ;(proxy as unknown as ConfiguredProxy).on('proxyReq', (proxyReq) => {
              proxyReq.setHeader(identityHeader, identityValue)
            })
          },
        },
      },
    },
    build: { outDir: 'dist', sourcemap: true },
  }
})
