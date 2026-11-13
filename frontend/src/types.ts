// API Types (mirror backend/app/schemas.py)

export type DocumentStatus =
  | 'received'
  | 'reading'
  | 'extraction_failed'
  | 'needs_review'
  | 'ready_for_approval'
  | 'duplicate_candidate'
  | 'approved'
  | 'bill_created'
  | 'attachment_pending'
  | 'recorded'
  | 'linked_existing'
  | 'remote_outcome_unknown';

export type ArrivalMethod = 'email' | 'upload';

export interface Arrival {
  id: string;
  arrived_at: string;
  method: ArrivalMethod;
  original_filename?: string | null;
  email_from?: string | null;
  email_subject?: string | null;
  email_received_at?: string | null;
  gmail_message_id?: string | null;
  uploaded_by?: string | null;
}

export interface DocumentListItem {
  id: string;
  status: DocumentStatus;
  status_reason?: string | null;
  created_at: string;
  revision: number;
  vendor_name?: string | null;
  invoice_number?: string | null;
  invoice_date?: string | null;
  total?: string | number | null;
  currency?: string | null;
  document_type?: string | null;
  property_id?: string | null;
  property_name?: string | null;
  qb_bill_id?: string | null;
  arrival_count: number;
  first_arrival_method?: ArrivalMethod | null;
  first_arrival_filename?: string | null;
  pending_question?: string | null;
  missing_fields: string[];
}

export interface CompanyStatus {
  connected: boolean;
  company_name?: string | null;
  realm_id?: string | null;
  expected_realm_id?: string | null;
  environment: string;
  verified: boolean;
  problem?: string | null;
}

export interface DocumentDetail {
  id: string;
  sha256_hash: string;
  content_type: string;
  file_size: number;
  status: DocumentStatus;
  status_reason?: string | null;
  revision: number;
  created_at: string;

  extracted_at?: string | null;
  extraction_model?: string | null;
  extracted_document_type?: string | null;
  extracted_vendor_name?: string | null;
  extracted_invoice_number?: string | null;
  extracted_invoice_date?: string | null;
  extracted_due_date?: string | null;
  extracted_total?: string | number | null;
  extracted_currency?: string | null;
  extracted_service_address?: string | null;
  extraction_uncertain_fields: string[];
  extraction_source_text?: string | null;

  effective_vendor_name?: string | null;
  effective_invoice_number?: string | null;
  effective_invoice_date?: string | null;
  effective_due_date?: string | null;
  effective_total?: string | number | null;
  effective_currency?: string | null;

  property_id?: string | null;
  property_name?: string | null;
  qb_vendor_id?: string | null;
  qb_vendor_name?: string | null;
  qb_account_id?: string | null;
  qb_account_name?: string | null;
  qb_class_id?: string | null;

  qb_bill_id?: string | null;
  qb_bill_doc_number?: string | null;
  qb_realm_id?: string | null;
  qb_attachment_id?: string | null;
  qb_bill_url?: string | null;
  qb_discrepancy?: string | null;
  last_accounting_check_at?: string | null;
  last_accounting_check_ok?: boolean | null;
  last_accounting_check_error?: string | null;

  todoist_task_id?: string | null;
  approved_at?: string | null;
  approved_by?: string | null;
  approved_revision?: number | null;

  arrivals: Arrival[];
  available_actions: string[];
  pending_question?: string | null;
  missing_fields: string[];
  company?: CompanyStatus | null;
}

export interface Property {
  id: string;
  name: string;
  address?: string | null;
}

export interface QBVendor { id: string; name: string; display_name: string }
export interface QBAccount { id: string; name: string; account_type: string; fully_qualified_name: string }
export interface QBClass { id: string; name: string }

export interface QBReferenceData {
  realm_id?: string | null;
  vendors: QBVendor[];
  accounts: QBAccount[];
  classes: QBClass[];
}

export interface QBConnectionStatus {
  configured: boolean;
  verified: boolean;
  redirect_uri?: string | null;
  connected: boolean;
  company_name?: string | null;
  realm_id?: string | null;
  environment: string;
  expected_realm_id?: string | null;
  realm_mismatch: boolean;
  last_error?: string | null;
  token_expires_at?: string | null;
}

export interface IntegrationStatus {
  configured: boolean;
  connected: boolean;
  error?: string | null;
  details?: Record<string, unknown> | null;
}

export interface AllIntegrationsStatus {
  gmail: IntegrationStatus;
  quickbooks: IntegrationStatus;
  openai: IntegrationStatus;
  todoist: IntegrationStatus;
}

export interface HistoryLink { label: string; href: string }

export interface HistoryEntry {
  id: string;
  event_type: string;
  occurred_at: string;
  actor: string;
  actor_kind: 'owner' | 'system';
  summary: string;
  links: HistoryLink[];
  details?: Record<string, unknown> | null;
}

export interface FieldComparison {
  local?: string | null;
  remote?: string | null;
  remote_name?: string | null;
  match?: boolean;
}

export interface DuplicateCandidate {
  id: string;
  realm_id?: string | null;
  qb_bill_id: string;
  qb_doc_number?: string | null;
  qb_vendor_name?: string | null;
  qb_total?: string | number | null;
  qb_currency?: string | null;
  qb_txn_date?: string | null;
  match_reason: string;
  match_fields?: string[] | null;
  match_details?: Record<string, FieldComparison> | null;
  found_at?: string | null;
  last_seen_at?: string | null;
  seen_count: number;
  resolved: boolean;
  resolution?: string | null;
  resolved_at?: string | null;
  resolved_by?: string | null;
  resolution_reason?: string | null;
}

export interface SearchResult {
  id: string;
  vendor_name?: string | null;
  invoice_number?: string | null;
  total?: string | number | null;
  currency?: string | null;
  status: DocumentStatus;
  property_name?: string | null;
  qb_bill_id?: string | null;
  qb_bill_doc_number?: string | null;
  invoice_date?: string | null;
  arrival_count: number;
  links: { record?: string | null; original?: string | null; quickbooks?: string | null };
}

export interface ProposedEdit {
  document_id: string;
  revision: number;
  field: string;
  field_label: string;
  update_key: string;
  current_value?: string | null;
  proposed_value: string;
  proposed_raw: unknown;
}

export interface SearchResponse {
  success: boolean;
  answer: string;
  intent?: string | null;
  interpreted_by?: string | null;
  documents: SearchResult[];
  selected_document_id?: string | null;
  needs_clarification: boolean;
  clarification_question?: string | null;
  pending_query?: string | null;
  history: HistoryEntry[];
  proposed_edit?: ProposedEdit | null;
  error?: string | null;
}

export interface GmailImportResult {
  message_id: string;
  attachment_filename: string;
  success: boolean;
  document_id?: string | null;
  is_duplicate: boolean;
  error?: string | null;
  skipped_reason?: string | null;
}

export interface GmailImportResponse {
  success: boolean;
  messages_found: number;
  attachments_processed: number;
  imported: number;
  already_processed: number;
  skipped: number;
  failed: number;
  results: GmailImportResult[];
  error?: string | null;
}

export interface ApprovalResponse {
  success: boolean;
  document_id: string;
  status: DocumentStatus;
  revision?: number | null;
  qb_bill_url?: string | null;
  qb_bill_id?: string | null;
  qb_bill_doc_number?: string | null;
  qb_attachment_id?: string | null;
  error?: string | null;
  requires_retry: boolean;
  retry_action?: string | null;
}

export interface VendorMapping {
  id: string;
  extracted_name: string;
  qb_vendor_id: string;
  qb_vendor_name: string;
  created_at: string;
}

export interface AccountMapping {
  id: string;
  category: string;
  qb_account_id: string;
  qb_account_name: string;
  created_at: string;
}
