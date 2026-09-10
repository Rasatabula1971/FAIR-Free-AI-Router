export type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };
export interface SolveOptions {
  priority?: 'P0' | 'P1' | 'P2' | 'P3' | 'P4';
  task_type?: string;
  quality_level?: 'commodity' | 'standard' | 'advanced' | 'high_impact_support';
  privacy_class?: 'PUBLIC' | 'INTERNAL' | 'CONFIDENTIAL' | 'RESTRICTED';
  expected_schema?: Record<string, JSONValue>;
  required_capabilities?: ('reasoning' | 'coding' | 'vision' | 'tool_calling' | 'structured_output' | 'embeddings')[];
  freshness_required?: boolean;
  cross_check_required?: boolean;
  validation?: Record<string, JSONValue>;
  evidence?: Record<string, JSONValue>[];
  source_policy?: Record<string, JSONValue>;
  cache_mode?: 'default' | 'bypass' | 'refresh';
  cache_ttl_seconds?: number;
}
export interface SolveResult {
  request_id: string;
  status: 'ACCEPTED' | 'ESCALATION_REQUIRED' | 'FAILED';
  reason_code: string;
  output: string | null;
  provider_id: string | null;
  model_id: string | null;
  verification_state: string;
  attempts: Record<string, JSONValue>[];
  cache_hit: boolean;
  cached_from_request_id: string | null;
  paid_inference_executed: false;
  [key: string]: unknown;
}
export interface CallOptions { signal?: AbortSignal }
export class FAIRClientError extends Error { code: string; statusCode: number | null }
export class FAIRClient {
  constructor(options: { baseUrl: string; clientId: string; apiKey: string; timeoutMs?: number; fetchImpl?: typeof fetch });
  solve(task: string, options?: SolveOptions, callOptions?: CallOptions): Promise<SolveResult>;
  providers(options?: CallOptions): Promise<Record<string, JSONValue>[]>;
  request(id: string, options?: CallOptions): Promise<Record<string, JSONValue>>;
  audit(id: string, options?: CallOptions): Promise<Record<string, JSONValue>[]>;
  feedback(id: string, values: { accepted?: boolean; rating?: number; reason?: string; correction_text?: string }, options?: CallOptions): Promise<Record<string, JSONValue>>;
  requestFeedback(id: string, options?: CallOptions): Promise<Record<string, JSONValue>>;
  clearCache(options?: CallOptions): Promise<{ entries_removed: number }>;
}
