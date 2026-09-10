import test from 'node:test';
import assert from 'node:assert/strict';
import { FAIRClient, FAIRClientError } from './index.js';

const envelope = { request_id: 'id', status: 'ESCALATION_REQUIRED', paid_inference_executed: false, attempts: [], output: null };
const client = (fetchImpl, options = {}) => new FAIRClient({ baseUrl: 'http://127.0.0.1:8000', clientId: 'alice', apiKey: 'private-key', fetchImpl, ...options });

test('solve binds identity, forwards contracts, and returns escalation without retry', async () => {
  let calls = 0;
  const sdk = client(async (url, init) => {
    calls++;
    assert.equal(url, 'http://127.0.0.1:8000/v1/solve');
    assert.equal(init.headers['X-API-Key'], 'private-key');
    assert.equal(init.redirect, 'error');
    assert.equal(JSON.parse(init.body).client_id, 'alice');
    assert.equal(JSON.parse(init.body).cache_mode, 'refresh');
    return Response.json(envelope);
  });
  assert.equal((await sdk.solve('Hello', { cache_mode: 'refresh' })).status, 'ESCALATION_REQUIRED');
  assert.equal(calls, 1);
  assert.throws(() => sdk.solve('Hello', { client_id: 'bob' }), FAIRClientError);
  assert.throws(() => sdk.audit('../system/stop'), FAIRClientError);
  assert.equal(JSON.stringify(sdk).includes('private-key'), false);
});

for (const status of [302, 401, 403, 429, 503]) {
  test(`HTTP ${status} preserves status without diagnostic or retries`, async () => {
    let calls = 0;
    const sdk = client(async () => { calls++; return new Response('private-key', { status }); });
    await assert.rejects(sdk.solve('Hello'), error => error.code === 'HTTP_ERROR' && error.statusCode === status && !String(error).includes('private-key'));
    assert.equal(calls, 1);
  });
}

test('rejects unsafe endpoint and invalid timeout', () => {
  for (const baseUrl of ['http://example.com', 'https://user:secret@example.com', 'https://example.com?key=secret']) assert.throws(() => client(fetch, { baseUrl }), FAIRClientError);
  assert.throws(() => client(fetch, { timeoutMs: NaN }), FAIRClientError);
});

test('malformed, paid and oversized responses fail closed', async () => {
  for (const body of ['broken', '{}', JSON.stringify({ ...envelope, paid_inference_executed: true }), ' '.repeat(2000001)]) {
    await assert.rejects(client(async () => new Response(body)).solve('Hello'), FAIRClientError);
  }
});

test('abort and deadline signals propagate without retry', async () => {
  const fetchImpl = async (url, { signal }) => new Promise((resolve, reject) => {
    if (signal.aborted) reject(new Error('private-key'));
    else signal.addEventListener('abort', () => reject(new Error('private-key')), { once: true });
  });
  const controller = new AbortController(); controller.abort();
  await assert.rejects(client(fetchImpl).solve('Hello', {}, { signal: controller.signal }), error => error.code === 'CANCELLED');
  await assert.rejects(client(fetchImpl, { timeoutMs: 10 }).solve('Hello'), error => error.code === 'TIMEOUT');
});

test('auxiliary routes preserve authenticated ownership paths', async () => {
  const paths = [];
  const sdk = client(async (url, init) => { paths.push([init.method, new URL(url).pathname]); return Response.json({}); });
  await sdk.providers(); await sdk.request('id'); await sdk.audit('id');
  await sdk.feedback('id', { accepted: true }); await sdk.requestFeedback('id'); await sdk.clearCache();
  assert.deepEqual(paths, [['GET', '/v1/providers'], ['GET', '/v1/requests/id'], ['GET', '/v1/requests/id/audit'], ['POST', '/v1/feedback'], ['GET', '/v1/requests/id/feedback'], ['DELETE', '/v1/cache']]);
});

test('real FAIR HTTP solve, cache, history, feedback and clearing', { skip: !process.env.FAIR_SDK_TEST_URL }, async () => {
  const sdk = new FAIRClient({ baseUrl: process.env.FAIR_SDK_TEST_URL, clientId: 'alice', apiKey: 'node-test-key' });
  const validation = { kind: 'arithmetic', expression: '2 + 2' };
  const first = await sdk.solve('Calculate 2 + 2', { validation });
  const second = await sdk.solve('Calculate 2 + 2', { validation });
  assert.equal(first.status, 'ACCEPTED'); assert.equal(first.output, '4');
  assert.equal(second.cache_hit, true); assert.equal(second.cached_from_request_id, first.request_id);
  assert.equal((await sdk.request(second.request_id)).cache_hit, true);
  assert.ok((await sdk.audit(second.request_id)).some(event => event.event_type === 'CACHE_HIT'));
  assert.equal((await sdk.feedback(first.request_id, { accepted: true })).accepted, true);
  assert.equal((await sdk.requestFeedback(first.request_id)).accepted, true);
  assert.equal((await sdk.clearCache()).entries_removed, 1);
  const invalid = new FAIRClient({ baseUrl: process.env.FAIR_SDK_TEST_URL, clientId: 'alice', apiKey: 'wrong-key' });
  await assert.rejects(invalid.providers(), error => error.statusCode === 401);
});
