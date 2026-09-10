import { isIP } from 'node:net';

export class FAIRClientError extends Error {
  constructor(code, statusCode = null) {
    super(code);
    this.name = 'FAIRClientError';
    this.code = code;
    this.statusCode = statusCode;
  }
}

function segment(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new FAIRClientError('INVALID_IDENTIFIER');
  return value;
}

export class FAIRClient {
  #url; #key; #client; #timeout; #fetch;
  constructor({ baseUrl, clientId, apiKey, timeoutMs = 180000, fetchImpl = globalThis.fetch }) {
    let url;
    try { url = new URL(baseUrl); } catch { throw new FAIRClientError('INVALID_ENDPOINT'); }
    const host = url.hostname.replace(/^\[|\]$/g, '');
    const local = host === 'localhost' || host === '::1' || (isIP(host) === 4 && host.startsWith('127.'));
    if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) || url.username || url.password || url.search || url.hash) throw new FAIRClientError('INVALID_ENDPOINT');
    if (typeof clientId !== 'string' || clientId.length < 1 || clientId.length > 128 || typeof apiKey !== 'string' || !/^[\x21-\x7E]+$/.test(apiKey)) throw new FAIRClientError('INVALID_CREDENTIAL_CONFIGURATION');
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647) throw new FAIRClientError('INVALID_TIMEOUT');
    this.#url = url.href.replace(/\/$/, ''); this.#key = apiKey; this.#client = clientId;
    this.#timeout = timeoutMs; this.#fetch = fetchImpl;
  }

  async #request(method, path, body, signal, solve = false) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeout);
    let reader;
    try {
      const response = await this.#fetch(this.#url + path, {
        method, redirect: 'error', credentials: 'omit',
        headers: { 'X-API-Key': this.#key, 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
      });
      if (!response.ok) {
        await response.body?.cancel();
        throw new FAIRClientError('HTTP_ERROR', response.status);
      }
      if (!response.body) throw new FAIRClientError('INVALID_RESPONSE');
      reader = response.body.getReader();
      const chunks = []; let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 2000000) throw new FAIRClientError('RESPONSE_TOO_LARGE');
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      let value;
      try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
      catch { throw new FAIRClientError('INVALID_RESPONSE'); }
      if (!value || typeof value !== 'object') throw new FAIRClientError('INVALID_RESPONSE');
      if (solve && (typeof value.request_id !== 'string' || !['ACCEPTED', 'ESCALATION_REQUIRED', 'FAILED'].includes(value.status) || value.paid_inference_executed !== false || !Array.isArray(value.attempts) || (value.status === 'ACCEPTED' && typeof value.output !== 'string'))) throw new FAIRClientError('INVALID_RESPONSE');
      return value;
    } catch (error) {
      if (error instanceof FAIRClientError) throw error;
      throw new FAIRClientError(signal?.aborted ? 'CANCELLED' : controller.signal.aborted ? 'TIMEOUT' : 'TRANSPORT_ERROR');
    } finally {
      clearTimeout(timer);
      if (reader) { try { await reader.cancel(); } catch { /* normalized above */ } reader.releaseLock(); }
    }
  }

  solve(task, options = {}, { signal } = {}) {
    if (typeof task !== 'string' || !task.length || !options || typeof options !== 'object' || Array.isArray(options) || Object.hasOwn(options, 'client_id') || Object.hasOwn(options, 'task')) throw new FAIRClientError('INVALID_REQUEST');
    return this.#request('POST', '/v1/solve', { ...options, client_id: this.#client, task }, signal, true);
  }
  providers({ signal } = {}) { return this.#request('GET', '/v1/providers', undefined, signal); }
  request(id, { signal } = {}) { return this.#request('GET', `/v1/requests/${segment(id)}`, undefined, signal); }
  audit(id, { signal } = {}) { return this.#request('GET', `/v1/requests/${segment(id)}/audit`, undefined, signal); }
  feedback(id, values, { signal } = {}) {
    if (!values || typeof values !== 'object' || Array.isArray(values) || Object.hasOwn(values, 'request_id')) throw new FAIRClientError('INVALID_REQUEST');
    return this.#request('POST', '/v1/feedback', { ...values, request_id: segment(id) }, signal);
  }
  requestFeedback(id, { signal } = {}) { return this.#request('GET', `/v1/requests/${segment(id)}/feedback`, undefined, signal); }
  clearCache({ signal } = {}) { return this.#request('DELETE', '/v1/cache', undefined, signal); }
}
