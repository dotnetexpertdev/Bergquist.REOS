import { useEffect, useState } from 'react';
import { getResetDemoPreview, resetDemo } from '../api';
import type { ResetDemoPreview, ResetDemoResult } from '../api';

interface ResetDemoProps {
  onClose: () => void;
}

// Confirmation dialog for wiping the demo: this app's documents, files and history, plus the
// QuickBooks bills the app itself created. Nothing is deleted until the word is typed.
export default function ResetDemo({ onClose }: ResetDemoProps) {
  const [preview, setPreview] = useState<ResetDemoPreview | null>(null);
  const [loadError, setLoadError] = useState('');
  const [includeQuickBooks, setIncludeQuickBooks] = useState(true);
  const [typed, setTyped] = useState('');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<ResetDemoResult | null>(null);

  useEffect(() => {
    getResetDemoPreview()
      .then((p) => { setPreview(p); setIncludeQuickBooks(p.quickbooks.available); })
      .catch((err) => setLoadError(err instanceof Error ? err.message : 'Could not load the reset preview'));
  }, []);

  const word = preview?.confirm_word || 'RESET';
  const bills = preview?.quickbooks.bills || [];

  const run = async () => {
    setWorking(true);
    setError('');
    try {
      setResult(await resetDemo(typed.trim(), includeQuickBooks));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reset failed');
    } finally {
      setWorking(false);
    }
  };

  // A full reload drops every cached list and document from the page.
  const finish = () => window.location.assign('/');

  return (
    <div className="overlay" onClick={(e) => { if (e.target === e.currentTarget && !working && !result) onClose(); }}>
      <div className="card assistant" role="dialog" aria-label="Reset demo data">
        <h3>Reset demo data</h3>
        {result ? (
          <>
            <div className="notice-success" role="status">
              Demo data cleared: {result.documents_deleted} document(s) and {result.files_deleted} stored file(s) removed
              {result.quickbooks.skipped ? '; QuickBooks was not touched'
                : `; ${result.quickbooks.bills_deleted} QuickBooks bill(s) and ${result.quickbooks.attachments_deleted} attachment(s) deleted`}
              {result.todoist_tasks_closed > 0 && `; ${result.todoist_tasks_closed} Todoist task(s) closed`}.
            </div>
            {result.files_left > 0 && (
              <p className="notice-warning">{result.files_left} file(s) were in use and could not be removed.</p>
            )}
            {result.backup && <p className="small muted">Backup saved on the server as {result.backup}.</p>}
            <button className="btn-primary" onClick={finish}>Done</button>
          </>
        ) : loadError ? (
          <>
            <div className="notice-error" role="alert">{loadError}</div>
            <button className="btn-secondary" onClick={onClose}>Close</button>
          </>
        ) : !preview ? (
          <div className="spinner" aria-label="Loading" />
        ) : (
          <div className="stack">
            <p>This permanently deletes, for a fresh demo:</p>
            <ul>
              <li>{preview.documents} document(s), {preview.arrivals} arrival(s) and {preview.history_events} history entries</li>
              <li>{preview.stored_files} stored file(s): originals and saved emails</li>
              {preview.todoist_tasks > 0 && <li>{preview.todoist_tasks} Todoist review task(s) will be closed</li>}
            </ul>
            <p className="small muted">
              Kept: Gmail and QuickBooks connections, properties, and vendor and account mappings. A backup zip of the
              data folder is saved on the server first.
            </p>

            {preview.quickbooks.available ? (
              <div className="confirm-panel">
                <label className="row">
                  <input type="checkbox" checked={includeQuickBooks} style={{ width: 'auto' }}
                    onChange={(e) => setIncludeQuickBooks(e.target.checked)} />
                  Also delete {bills.length} QuickBooks bill(s) this app created
                  {preview.quickbooks.company_name && ` in ${preview.quickbooks.company_name}`}
                </label>
                {bills.length > 0 && (
                  <ul className="small">
                    {bills.map((b) => (
                      <li key={b.id}>{b.vendor_name || 'Unknown vendor'} · bill {b.doc_number || b.id}
                        {b.total && ` · ${b.total}`}{b.txn_date && ` · ${b.txn_date}`}</li>
                    ))}
                  </ul>
                )}
                <p className="small muted">Bills entered in QuickBooks by hand are never deleted.</p>
              </div>
            ) : (
              <p className="notice-warning">
                QuickBooks will not be cleaned: {preview.quickbooks.problem || 'not connected'}. Only the data in this
                app will be deleted.
              </p>
            )}

            <label>Type {word} to confirm
              <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" disabled={working}
                aria-label={`Type ${word} to confirm`} />
            </label>
            {error && <div className="notice-error" role="alert">{error}</div>}
            <div className="row">
              <button className="btn-danger" onClick={run} disabled={working || typed.trim() !== word}>
                {working ? 'Deleting…' : 'Delete demo data'}
              </button>
              <button className="btn-secondary" onClick={onClose} disabled={working}>Cancel</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
