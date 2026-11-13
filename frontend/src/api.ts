// API client. All calls go to the same origin (Vite proxies /api in development).
import type {
  AccountMapping, AllIntegrationsStatus, ApprovalResponse, DocumentDetail, DocumentListItem, DuplicateCandidate,
  GmailImportResponse, HistoryEntry, IntegrationStatus, Property, QBConnectionStatus, QBReferenceData,
  SearchResponse, VendorMapping,
} from './types';

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  status: number;
  retryable: boolean;
  fieldErrors: Record<string, string>;
  constructor(message: string, status: number, retryable = false, fieldErrors: Record<string, string> = {}) {
    super(message);
    this.status = status;
    this.retryable = retryable;
    this.fieldErrors = fieldErrors;
  }
}

// The server compares the header with the csrf_token cookie. The cookie is shared by every
// tab and may have been renewed by another one, so the cookie's current value wins.
function currentCsrfToken(): string | null {
  const match = typeof document !== 'undefined' ? document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/) : null;
  return match ? decodeURIComponent(match[1]) : csrfToken;
}

async function request(url: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers);
  const method = (options.method || 'GET').toUpperCase();
  const token = currentCsrfToken();
  if (token && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    headers.set('X-CSRF-Token', token);
  }
  return fetch(url, { ...options, headers, credentials: 'include' });
}

async function toError(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => ({}));
  const detail = body.detail ?? body.error;
  if (detail && typeof detail === 'object') {
    return new ApiError(detail.message || 'Request failed', response.status, !!detail.retryable,
      detail.field_errors || {});
  }
  return new ApiError(typeof detail === 'string' ? detail : response.statusText || 'Request failed', response.status);
}

async function json<T>(url: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await request(url, { ...options, headers });
  if (!response.ok) throw await toError(response);
  return response.json() as Promise<T>;
}

const post = <T>(url: string, body?: unknown) =>
  json<T>(url, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });

// Auth
export async function login(username: string, password: string) {
  const res = await post<{ success: boolean; username?: string; message?: string; csrf_token?: string }>(
    '/api/auth/login', { username, password });
  if (res.csrf_token) setCsrfToken(res.csrf_token);
  return res;
}

export async function logout() {
  await request('/api/auth/logout', { method: 'POST' });
  setCsrfToken(null);
}

export async function getSession() {
  const res = await json<{ authenticated: boolean; username?: string; csrf_token?: string }>('/api/auth/session');
  if (res.csrf_token) setCsrfToken(res.csrf_token);
  return res;
}

// Documents
export const getDocuments = (statusFilter?: string) =>
  json<DocumentListItem[]>(`/api/documents${statusFilter ? `?status_filter=${encodeURIComponent(statusFilter)}` : ''}`);
export const getNeedsReview = () => json<DocumentListItem[]>('/api/documents/needs-review');
export const getDocument = (id: string) => json<DocumentDetail>(`/api/documents/${id}`);
export const getDocumentHistory = (id: string) => json<HistoryEntry[]>(`/api/documents/${id}/history`);

export interface DocumentUpdate {
  revision: number;
  reviewed_document_type?: string;
  reviewed_vendor_name?: string;
  reviewed_invoice_number?: string;
  reviewed_invoice_date?: string;
  reviewed_due_date?: string;
  reviewed_total?: string;
  reviewed_currency?: string;
  property_id?: string;
  qb_vendor_id?: string;
  qb_account_id?: string;
  qb_class_id?: string;
  source?: 'typed' | 'voice';
}

export const updateDocument = (id: string, data: DocumentUpdate) =>
  post<{ success: boolean; revision: number; status: string; pending_question?: string }>(
    `/api/documents/${id}/update`, data);

export async function uploadDocument(file: File, attemptId: string) {
  const form = new FormData();
  form.append('file', file);
  form.append('attempt_id', attemptId);
  const response = await request('/api/documents/upload', { method: 'POST', body: form });
  if (!response.ok) throw await toError(response);
  return response.json() as Promise<{
    success: boolean; document_id?: string; message?: string; is_duplicate?: boolean; retried_attempt?: boolean;
  }>;
}

export const resolveCopy = (id: string, revision: number, resolution: 'same' | 'different') =>
  post<{ success: boolean; revision: number; status: string }>(`/api/documents/${id}/resolve-copy`,
    { revision, resolution });

export const retryExtraction = (id: string) => post<{ success: boolean }>(`/api/documents/${id}/retry-extraction`);

// Gmail
export const getGmailStatus = () => json<IntegrationStatus>('/api/gmail/status');
export async function authorizeGmail() {
  const res = await json<{ auth_url: string }>('/api/gmail/authorize');
  window.location.href = res.auth_url;
}
export const disconnectGmail = () => post<{ success: boolean }>('/api/gmail/disconnect');
export const importGmailMessages = () => post<GmailImportResponse>('/api/gmail/import');

// QuickBooks
export const getQBStatus = () => json<QBConnectionStatus>('/api/quickbooks/status');
export async function authorizeQuickBooks() {
  const res = await json<{ auth_url: string }>('/api/quickbooks/authorize');
  window.location.href = res.auth_url;
}
export const disconnectQuickBooks = () => post<{ success: boolean }>('/api/quickbooks/disconnect');
export const getQBReferenceData = () => json<QBReferenceData>('/api/quickbooks/reference-data');
export const checkDuplicates = (id: string) =>
  post<{ success: boolean; candidates_found: number; new_candidates: string[] }>(`/api/quickbooks/${id}/check-duplicates`);
export const getDuplicateCandidates = (id: string) =>
  json<DuplicateCandidate[]>(`/api/quickbooks/${id}/duplicate-candidates`);
export const resolveDuplicate = (id: string, candidateId: string, resolution: 'linked' | 'dismissed', reason?: string) =>
  post<{ success: boolean; status: string }>(`/api/quickbooks/${id}/resolve-duplicate`,
    { candidate_id: candidateId, resolution, reason });
export const approveDocument = (id: string, revision: number) =>
  post<ApprovalResponse>(`/api/quickbooks/${id}/approve`, { revision });
export const reconcileDocument = (id: string) => post<ApprovalResponse>(`/api/quickbooks/${id}/reconcile`);
export const retryAttachment = (id: string) => post<ApprovalResponse>(`/api/quickbooks/${id}/retry-attachment`);

// Properties
export const getProperties = () => json<Property[]>('/api/properties');
export const createProperty = (name: string, address?: string) =>
  post<Property>('/api/properties', { name, address });

// Integrations
export const getIntegrationsStatus = () => json<AllIntegrationsStatus>('/api/integrations/status');

// Assistant
export const search = (query: string, contextDocumentId?: string, selectDocumentId?: string) =>
  post<SearchResponse>('/api/search', {
    query, context_document_id: contextDocumentId ?? null, select_document_id: selectDocumentId ?? null,
  });

const AUDIO_EXT: Record<string, string> = { 'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'mp4' };

export async function transcribe(audio: Blob) {
  const type = (audio.type || 'audio/webm').split(';')[0];
  const form = new FormData();
  form.append('audio', new File([audio], `turn.${AUDIO_EXT[type] || 'webm'}`, { type }));
  const response = await request('/api/transcribe', { method: 'POST', body: form });
  if (!response.ok) throw await toError(response);
  return response.json() as Promise<{ success: boolean; transcript?: string; error?: string }>;
}

// Mappings
export const getVendorMappings = () => json<VendorMapping[]>('/api/mappings/vendors');
export const createVendorMapping = (extractedName: string, qbVendorId: string, qbVendorName: string) =>
  post<VendorMapping>('/api/mappings/vendors',
    { extracted_name: extractedName, qb_vendor_id: qbVendorId, qb_vendor_name: qbVendorName });
export const deleteVendorMapping = (id: string) =>
  json<{ success: boolean }>(`/api/mappings/vendors/${id}`, { method: 'DELETE' });
export const getAccountMappings = () => json<AccountMapping[]>('/api/mappings/accounts');
export const createAccountMapping = (category: string, qbAccountId: string, qbAccountName: string) =>
  post<AccountMapping>('/api/mappings/accounts',
    { category, qb_account_id: qbAccountId, qb_account_name: qbAccountName });
export const deleteAccountMapping = (id: string) =>
  json<{ success: boolean }>(`/api/mappings/accounts/${id}`, { method: 'DELETE' });
