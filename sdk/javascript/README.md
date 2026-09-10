# FAIR JavaScript client

Node.js 22+ ESM client for the authenticated FAIR API, with TypeScript declarations and no npm
dependencies. This package is private and distributed from the repository.

```javascript
import { FAIRClient } from '@fair-router/client';
const fair = new FAIRClient({
  baseUrl: 'http://127.0.0.1:8000', clientId: 'my-app', apiKey: process.env.FAIR_API_KEY,
});
const result = await fair.solve('Compute 2 + 2', {
  validation: { kind: 'arithmetic', expression: '2 + 2' },
});
if (result.status === 'ACCEPTED') console.log(result.output);
```

Use a FAIR client key, never a provider key. Keep keys in trusted server applications. Solve
options use API snake_case names. `FAIRClientError` exposes `code` and `statusCode` without raw
error bodies. No automatic retries or provider activation occur. Escalation is returned as a
normal result; inspect `status` before consuming output. All operations accept an optional
AbortSignal; for solve pass it in a third `{signal}` argument. Default total timeout: 180 seconds.

Methods: `solve`, `providers`, `request`, `audit`, `feedback`, `requestFeedback`, `clearCache`.
See the repository's `docs/SDK_AND_CACHE.md` for complete examples, cache controls and limitations.
Run `npm test` for transport tests. The repository pytest suite also runs the real HTTP integration.
