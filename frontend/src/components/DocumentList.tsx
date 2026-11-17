import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { DOCUMENTS_CHANGED, getDocuments, getGmailStatus, getNeedsReview, importGmailMessages, uploadDocument } from '../api';
import type { DocumentListItem, GmailImportResponse, IntegrationStatus } from '../types';
import StatusBadge from './StatusBadge';

type FilterTab = 'all' | 'needs_review' | 'ready';

const CATEGORY_LABELS: Record<string, string> = {
  bill: 'Bill', receipt: 'Receipt', bank_statement: 'Bank statement', lease: 'Lease', tax_notice: 'Tax notice',
  insurance: 'Insurance', other: 'Other document',
};

function newAttemptId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, (c) =>
    (Number(c) ^ (Math.random() * 16) >> (Number(c) / 4)).toString(16));
}

const REVIEW_STATES = ['needs_review', 'extraction_failed', 'duplicate_candidate', 'possible_copy',
  'attachment_pending', 'remote_outcome_unknown'];

function greeting(): string {
  const hour = new Date().getHours();
  return hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
}

// One line icon per document type (decorative).
function DocIcon({ kind }: { kind: string }) {
  const paths: Record<string, JSX.Element> = {
    bill: <><path d="M6 2h9l5 5v15H6z" /><path d="M14 2v6h6" /><path d="M9 13h8M9 17h5" /></>,
    receipt: <><path d="M6 2h12v20l-3-2-3 2-3-2-3 2z" /><path d="M9 8h6M9 12h6" /></>,
    bank_statement: <><path d="M3 10l9-6 9 6" /><path d="M5 10v9M9.5 10v9M14.5 10v9M19 10v9M3 21h18" /></>,
    lease: <><path d="M3 11l9-8 9 8" /><path d="M5 10v11h14V10" /><path d="M10 21v-6h4v6" /></>,
    tax_notice: <><circle cx="12" cy="12" r="9" /><path d="M9 9h.01M15 15h.01M15.5 8.5l-7 7" /></>,
    insurance: <><path d="M12 2l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V5z" /><path d="M9 12l2 2 4-4" /></>,
    other: <><path d="M6 2h9l5 5v15H6z" /><path d="M14 2v6h6" /></>,
  };
  return (
    <span className={`doc-icon kind-${kind}`} aria-hidden="true">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
        strokeLinecap="round" strokeLinejoin="round" focusable="false">{paths[kind] || paths.other}</svg>
    </span>
  );
}

const money = (amount: DocumentListItem['total'], currency?: string | null) => {
  if (amount === null || amount === undefined) return 'Amount unknown';
  return `${currency || '(currency unknown)'} ${amount}`;
};

export default function DocumentList() {
  const [documents, setDocuments] = useState<DocumentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState<FilterTab>('all');
  const [maxMb, setMaxMb] = useState(25);
  const [stats, setStats] = useState<{ total: number; review: number; ready: number; booked: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [uploadResult, setUploadResult] = useState<{ id: string; message: string } | null>(null);
  const [pending, setPending] = useState<{ file: File; attemptId: string } | null>(null);

  const [gmailStatus, setGmailStatus] = useState<IntegrationStatus | null>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<GmailImportResponse | null>(null);

  const loadDocuments = useCallback(async () => {
    setError('');
    try {
      const docs = filter === 'needs_review' ? await getNeedsReview()
        : filter === 'ready' ? await getDocuments('ready_for_approval') : await getDocuments();
      setDocuments(docs);
      if (filter === 'all') {
        setStats({
          total: docs.length,
          review: docs.filter((d) => REVIEW_STATES.includes(d.status)).length,
          ready: docs.filter((d) => d.status === 'ready_for_approval').length,
          booked: docs.filter((d) => d.status === 'recorded' || d.status === 'linked_existing').length,
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load documents');
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    loadDocuments();
  }, [loadDocuments]);

  // The assistant can import from the inbox too; reload when it has.
  useEffect(() => {
    const reload = () => { loadDocuments(); };
    window.addEventListener(DOCUMENTS_CHANGED, reload);
    return () => window.removeEventListener(DOCUMENTS_CHANGED, reload);
  }, [loadDocuments]);

  useEffect(() => {
    getGmailStatus().then(setGmailStatus).catch(() => setGmailStatus(null));
    fetch('/api/health', { credentials: 'include' }).then((r) => r.json())
      .then((h) => h.max_upload_size_mb && setMaxMb(h.max_upload_size_mb)).catch(() => undefined);
  }, []);

  // Keep the list fresh while bills are being read.
  useEffect(() => {
    if (documents.some((d) => d.status === 'received' || d.status === 'reading')) {
      const t = setTimeout(loadDocuments, 1500);
      return () => clearTimeout(t);
    }
  }, [documents, loadDocuments]);

  const send = async (file: File, attemptId: string): Promise<string | null> => {
    setUploading(true);
    setUploadError('');
    setUploadResult(null);
    try {
      const result = await uploadDocument(file, attemptId);
      setPending(null);
      setUploadResult({ id: result.document_id!, message: result.message || 'Document uploaded.' });
      loadDocuments();
      return result.document_id || null;
    } catch (err) {
      // Keep the same attempt ID so a retry of this interrupted upload counts once.
      setPending({ file, attemptId });
      setUploadError(err instanceof Error ? err.message : 'Upload failed');
      return null;
    } finally {
      setUploading(false);
    }
  };

  // One or several files, sent one after another. Each keeps its own attempt ID.
  const sendAll = async (files: File[]) => {
    if (!files.length || uploading) return;
    if (files.length === 1) {
      await send(files[0], newAttemptId());
      return;
    }
    let received = 0;
    let lastId: string | null = null;
    for (const file of files) {
      const id = await send(file, newAttemptId());
      if (!id) return; // the error and its retry button are already showing for this file
      received += 1;
      lastId = id;
    }
    if (lastId) setUploadResult({ id: lastId, message: `${received} files received. Most recent:` });
  };

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = '';
    sendAll(files);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    sendAll(Array.from(e.dataTransfer.files || []));
  };

  const handleImport = async () => {
    setImporting(true);
    setImportResult(null);
    try {
      const result = await importGmailMessages();
      setImportResult(result);
      loadDocuments();
    } catch (err) {
      setImportResult({ success: false, messages_found: 0, attachments_processed: 0, imported: 0,
        already_processed: 0, skipped: 0, failed: 0, results: [],
        error: err instanceof Error ? err.message : 'Import failed' });
    } finally {
      setImporting(false);
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="eyebrow">{new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}</div>
          <h2>{greeting()}</h2>
          <p>Everything that came in, what was done with it, and what is waiting for you.</p>
        </div>
      </div>

      {stats && (
        <div className="stats" aria-label="Summary">
          <div className="stat"><div className="stat-value">{stats.total}</div><div className="stat-label">Documents</div></div>
          <div className="stat" style={{ ['--tone' as string]: 'var(--brass)' }}><div className="stat-value">{stats.review}</div><div className="stat-label">Waiting on you</div></div>
          <div className="stat" style={{ ['--tone' as string]: 'var(--info)' }}><div className="stat-value">{stats.ready}</div><div className="stat-label">Prepared to approve</div></div>
          <div className="stat" style={{ ['--tone' as string]: 'var(--success)' }}><div className="stat-value">{stats.booked}</div><div className="stat-label">In QuickBooks</div></div>
        </div>
      )}

      <section className="card intake" aria-label="Add bills">
        <div className="intake-head">
          <div>
            <div className="intake-title">Add a document</div>
            <div className="intake-sub">Drop files below, choose them from this device, or pull in labeled email.</div>
          </div>
          <div className="row wrap intake-actions">
            {gmailStatus?.connected ? (
              <button className="btn-secondary" onClick={handleImport} disabled={importing}>
                {importing ? 'Importing…' : 'Import From Inbox'}
              </button>
            ) : (
              <span className="muted small">Gmail not connected — <Link to="/settings">connect it in Settings</Link> to import from your inbox.</span>
            )}
            <button className="btn-secondary" onClick={() => { setLoading(true); loadDocuments(); }} disabled={loading}>Refresh</button>
          </div>
        </div>

        <div className={`dropzone${dragging ? ' dragging' : ''}${uploading ? ' busy' : ''}`} aria-label="Drop files here"
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)} onDrop={handleDrop}
          onClick={(e) => { if (!(e.target as HTMLElement).closest('label')) fileInputRef.current?.click(); }}>
          <span className="dropzone-icon" aria-hidden="true">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
              strokeLinecap="round" strokeLinejoin="round" focusable="false">
              <path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
            </svg>
          </span>
          <div className="dropzone-title">{dragging ? 'Release to upload' : 'Drag and drop bills here'}</div>
          <div className="dropzone-or">or</div>
          <label className="btn-primary file-label">
            {uploading ? 'Uploading…' : 'Upload bill'}
            <input ref={fileInputRef} type="file" multiple accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
              onChange={handleFile} disabled={uploading} className="visually-hidden" aria-label="Upload bill" />
          </label>
          <p className="muted small">Supported formats: PDF, JPEG, PNG · maximum {maxMb} MB.</p>
        </div>

        {uploadResult && (
          <div className="notice-success" role="status">
            {uploadResult.message} <Link to={`/documents/${uploadResult.id}`}>Open document</Link>
          </div>
        )}
        {uploadError && (
          <div className="notice-error" role="alert">
            {uploadError}
            {pending && (
              <> <button className="btn-secondary" onClick={() => send(pending.file, pending.attemptId)} disabled={uploading}>Retry upload</button></>
            )}
          </div>
        )}
        {importResult && (
          <div className={importResult.success ? 'notice-success' : 'notice-error'} role="status" aria-label="Import results">
            {importResult.error || (
              `Found ${importResult.messages_found} labeled message(s). Imported: ${importResult.imported}, ` +
              `already processed: ${importResult.already_processed}, skipped: ${importResult.skipped}, failed: ${importResult.failed}.`
            )}
            {importResult.results.filter((r) => r.error || r.skipped_reason).map((r) => (
              <div key={`${r.message_id}-${r.attachment_filename}`} className="small">
                {r.attachment_filename || r.message_id}: {r.error || r.skipped_reason}
              </div>
            ))}
            {(importResult.failed > 0 || importResult.error) && (
              <button className="btn-secondary" onClick={handleImport} disabled={importing}>Retry import</button>
            )}
          </div>
        )}
      </section>

      <div className="segmented" role="group" aria-label="Filter documents">
        {([['all', 'All'], ['needs_review', 'Needs review'], ['ready', 'Ready to approve']] as [FilterTab, string][]).map(([tab, label]) => (
          <button key={tab} className={filter === tab ? 'btn-primary' : 'btn-secondary'} aria-pressed={filter === tab}
            onClick={() => { setLoading(true); setFilter(tab); }}>
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}><div className="spinner" aria-label="Loading" /></div>
      ) : error ? (
        <div className="card notice-error" role="alert">{error}</div>
      ) : documents.length === 0 ? (
        <div className="card empty-state">No documents yet. Upload a bill or import from your inbox.</div>
      ) : (
        <ul className="plain doc-list" aria-label="Documents">
          {documents.map((doc) => (
            <li key={doc.id}>
              <Link to={`/documents/${doc.id}`} className="card doc-row">
                <DocIcon kind={doc.document_category || 'bill'} />
                <div style={{ flex: 1, minWidth: '200px' }}>
                  <div className="doc-title">{doc.vendor_name || doc.first_arrival_filename || 'Unknown vendor'}</div>
                  <div className="muted small">
                    {doc.document_category && doc.document_category !== 'bill' && (
                      <span className="type-tag">{CATEGORY_LABELS[doc.document_category]} · </span>
                    )}
                    {doc.invoice_number ? `#${doc.invoice_number} · ` : ''}
                    {new Date(doc.created_at + 'Z').toLocaleDateString()}
                    {` · ${doc.property_name || 'No property'}`}
                    {` · ${doc.arrival_count} arrival${doc.arrival_count === 1 ? '' : 's'}`}
                  </div>
                  {doc.pending_question && <div className="question small">{doc.pending_question}</div>}
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div className="doc-amount">{money(doc.total, doc.currency)}</div>
                  <StatusBadge status={doc.status} />
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
