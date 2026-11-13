import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { getDocuments, getGmailStatus, getNeedsReview, importGmailMessages, uploadDocument } from '../api';
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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load documents');
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    loadDocuments();
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

  const send = async (file: File, attemptId: string) => {
    setUploading(true);
    setUploadError('');
    setUploadResult(null);
    try {
      const result = await uploadDocument(file, attemptId);
      setPending(null);
      setUploadResult({ id: result.document_id!, message: result.message || 'Document uploaded.' });
      loadDocuments();
    } catch (err) {
      // Keep the same attempt ID so a retry of this interrupted upload counts once.
      setPending({ file, attemptId });
      setUploadError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) send(file, newAttemptId());
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
      <section className="card" aria-label="Add bills">
        <div className="row wrap">
          <label className="btn-primary file-label">
            {uploading ? 'Uploading…' : 'Upload bill'}
            <input type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
              onChange={handleFile} disabled={uploading} className="visually-hidden" aria-label="Upload bill" />
          </label>
          {gmailStatus?.connected ? (
            <button className="btn-secondary" onClick={handleImport} disabled={importing}>
              {importing ? 'Importing…' : 'Import demo emails'}
            </button>
          ) : (
            <span className="muted small">Gmail not connected — <Link to="/settings">connect it in Settings</Link> to import demo emails.</span>
          )}
          <button className="btn-secondary" onClick={() => { setLoading(true); loadDocuments(); }} disabled={loading}>Refresh</button>
        </div>
        <p className="muted small">Supported formats: PDF, JPEG, PNG · maximum {maxMb} MB.</p>

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

      <div className="row" role="group" aria-label="Filter documents">
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
        <div className="card muted" style={{ textAlign: 'center' }}>No documents yet. Upload a bill or import demo emails.</div>
      ) : (
        <ul className="plain doc-list" aria-label="Documents">
          {documents.map((doc) => (
            <li key={doc.id}>
              <Link to={`/documents/${doc.id}`} className="card doc-row">
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
