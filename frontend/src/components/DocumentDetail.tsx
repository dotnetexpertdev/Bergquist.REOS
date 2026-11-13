import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ApiError, approveDocument, checkDuplicates, getDocument, getDocumentHistory, getDuplicateCandidates,
  getProperties, getQBReferenceData, reconcileDocument, resolveCopy, resolveDuplicate, retryAttachment,
  retryExtraction, updateDocument,
} from '../api';
import type {
  DocumentDetail as DocumentDetailType, DuplicateCandidate, HistoryEntry, Property, QBReferenceData,
} from '../types';
import StatusBadge from './StatusBadge';

type Notice = { kind: 'error' | 'info' | 'success'; text: string } | null;

const CATEGORY_LABELS: Record<string, string> = {
  bill: 'Bill / invoice', receipt: 'Receipt (already paid)', bank_statement: 'Bank or card statement',
  lease: 'Lease or rental agreement', tax_notice: 'Tax or assessment notice', insurance: 'Insurance document',
  other: 'Other document',
};

const FIELD_LABELS: Record<string, string> = {
  vendor_name: 'Vendor', total: 'Total', currency: 'Currency', invoice_number: 'Invoice number',
  invoice_date: 'Invoice date', property: 'Property', qb_vendor: 'QuickBooks vendor', qb_account: 'Expense account',
};

function Unknown() {
  return <span className="unknown">Unknown — needs review</span>;
}

function errorText(err: unknown, fallback: string) {
  return err instanceof Error ? err.message : fallback;
}

const fmt = (iso?: string | null) => (iso ? new Date(iso.endsWith('Z') ? iso : `${iso}Z`).toLocaleString() : '');

export default function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const [doc, setDoc] = useState<DocumentDetailType | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [candidates, setCandidates] = useState<DuplicateCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState('');

  const [properties, setProperties] = useState<Property[]>([]);
  const [qbData, setQBData] = useState<QBReferenceData | null>(null);
  const [qbDataError, setQbDataError] = useState('');

  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [dismissing, setDismissing] = useState<string | null>(null);
  const [dismissReason, setDismissReason] = useState('');
  const [form, setForm] = useState({
    document_type: 'bill', vendor_name: '', invoice_number: '', invoice_date: '', due_date: '', total: '', currency: '',
    property_id: '', qb_vendor_id: '', qb_account_id: '', qb_class_id: '',
  });

  const load = useCallback(async () => {
    if (!id) return;
    setLoadError('');
    try {
      const [d, h, c] = await Promise.all([getDocument(id), getDocumentHistory(id), getDuplicateCandidates(id)]);
      setDoc(d);
      setHistory(h);
      setCandidates(c);
      setForm({
        document_type: d.document_category || 'bill', vendor_name: d.effective_vendor_name || '', invoice_number: d.effective_invoice_number || '',
        invoice_date: d.effective_invoice_date || '', due_date: d.effective_due_date || '',
        total: d.effective_total != null ? String(d.effective_total) : '', currency: d.effective_currency || '',
        property_id: d.property_id || '', qb_vendor_id: d.qb_vendor_id || '', qb_account_id: d.qb_account_id || '',
        qb_class_id: d.qb_class_id || '',
      });
    } catch (err) {
      setLoadError(errorText(err, 'Failed to load document'));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    getProperties().then(setProperties).catch(() => setProperties([]));
    getQBReferenceData().then(setQBData).catch((e) => setQbDataError(errorText(e, 'QuickBooks unavailable')));
  }, []);

  // Refresh while the server is still reading the bill.
  useEffect(() => {
    if (doc && (doc.status === 'received' || doc.status === 'reading')) {
      const t = setTimeout(load, 1500);
      return () => clearTimeout(t);
    }
  }, [doc, load]);

  const run = async (name: string, action: () => Promise<Notice | void>) => {
    setBusy(name);
    setNotice(null);
    try {
      const result = await action();
      if (result) setNotice(result);
    } catch (err) {
      const retry = err instanceof ApiError && err.retryable ? ' You can retry.' : '';
      setNotice({ kind: 'error', text: errorText(err, 'Action failed') + retry });
    } finally {
      setBusy('');
      await load();
    }
  };

  const handleSave = () => run('save', async () => {
    if (!doc || !id) return;
    await updateDocument(id, {
      revision: doc.revision,
      reviewed_document_type: form.document_type !== (doc.document_category || 'bill') ? form.document_type : undefined,
      reviewed_vendor_name: form.vendor_name || undefined,
      reviewed_invoice_number: form.invoice_number || undefined,
      reviewed_invoice_date: form.invoice_date || undefined,
      reviewed_due_date: form.due_date || undefined,
      reviewed_total: form.total || undefined,
      reviewed_currency: form.currency || undefined,
      property_id: form.property_id || undefined,
      qb_vendor_id: form.qb_vendor_id || undefined,
      qb_account_id: form.qb_account_id || undefined,
      qb_class_id: form.qb_class_id || undefined,
      source: 'typed',
    });
    setEditing(false);
    return { kind: 'success', text: 'Correction saved. Nothing was sent to QuickBooks.' };
  });

  const handleCreateBill = () => run('approve', async () => {
    if (!doc || !id) return;
    setConfirming(false);
    const r = await approveDocument(id, doc.revision);
    if (r.status === 'recorded') return { kind: 'success', text: `Unpaid bill ${r.qb_bill_doc_number || r.qb_bill_id} created with the original attached.` };
    return { kind: r.success ? 'info' : 'error', text: r.error || `Status: ${r.status}` };
  });

  if (loading) {
    return <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}><div className="spinner" aria-label="Loading" /></div>;
  }
  if (loadError || !doc || !id) {
    return (
      <div className="card notice-error" role="alert">
        {loadError || 'Document not found'}<br /><Link to="/">Back to list</Link>
      </div>
    );
  }

  const can = (a: string) => doc.available_actions.includes(a);
  const category = doc.document_category || 'bill';
  const isBill = category === 'bill';
  const categories = doc.document_categories && Object.keys(doc.document_categories).length
    ? doc.document_categories : CATEGORY_LABELS;
  const copy = doc.copy_of;
  const missing = new Set(doc.missing_fields);
  const openCandidates = candidates.filter((c) => !c.resolved);
  const resolvedCandidates = candidates.filter((c) => c.resolved);
  const company = doc.company;
  const fileUrl = `/api/documents/${id}/file`;
  const accountName = qbData?.accounts.find((a) => a.id === doc.qb_account_id)?.fully_qualified_name
    || doc.qb_account_name || doc.qb_account_id;
  const vendorName = qbData?.vendors.find((v) => v.id === doc.qb_vendor_id)?.display_name
    || doc.qb_vendor_name || doc.qb_vendor_id;
  const className = qbData?.classes.find((c) => c.id === doc.qb_class_id)?.name || doc.qb_class_id;
  const value = (v: unknown, field?: string) =>
    v === null || v === undefined || v === '' ? (field && missing.has(field) ? <Unknown /> : <span>—</span>) : <span>{String(v)}</span>;

  return (
    <div>
      <div className="detail-header">
        <div>
          <Link to="/" className="back-link">← Back to list</Link>
          <h2 style={{ margin: '0.5rem 0 0' }}>{doc.effective_vendor_name || 'Unknown vendor'}</h2>
        </div>
        <StatusBadge status={doc.status} />
      </div>

      {doc.pending_question && (
        <div className="card notice-warning" role="status" aria-label="Question for you">
          <strong>Action needed:</strong> {doc.pending_question}
        </div>
      )}
      {copy && (
        <div className={`card ${copy.confirmed ? 'notice-info' : 'notice-warning'}`} role="region" aria-label="Copy of a stored bill">
          {copy.confirmed ? (
            <p style={{ margin: 0 }}>
              This file is a confirmed copy of <Link to={`/documents/${copy.id}`}>{copy.title}</Link>
              {' '}({copy.status.replace(/_/g, ' ')}). That record carries the accounting; nothing is posted from this one.
            </p>
          ) : (
            <>
              <p style={{ marginTop: 0 }}>
                This looks like another copy of <Link to={`/documents/${copy.id}`}>{copy.title}</Link>
                {copy.total ? ` · ${copy.currency || ''} ${copy.total}` : ''}
                {copy.invoice_date ? ` · ${copy.invoice_date}` : ''} · {copy.status.replace(/_/g, ' ')}
                {copy.qb_bill_id ? ` · QuickBooks bill ${copy.qb_bill_doc_number || copy.qb_bill_id}` : ''}.
                {' '}It is the same invoice in a different file (for example a phone photo of the paper bill).
              </p>
              {can('resolve_copy') && (
                <div className="row wrap">
                  <button className="btn-primary" disabled={!!busy} onClick={() => run('copy-same', async () => {
                    await resolveCopy(id, doc.revision, 'same');
                    return { kind: 'success', text: 'Linked as a copy. No second bill will be created.' };
                  })}>Same invoice: link as copy</button>
                  <button className="btn-secondary" disabled={!!busy} onClick={() => run('copy-different', async () => {
                    await resolveCopy(id, doc.revision, 'different');
                    return { kind: 'info', text: 'Kept as a separate bill. QuickBooks is still checked before posting.' };
                  })}>Different invoice</button>
                </div>
              )}
            </>
          )}
        </div>
      )}
      {doc.status_reason && <div className="card notice-info" role="status">{doc.status_reason}</div>}
      {doc.qb_discrepancy && <div className="card notice-warning" role="status">{doc.qb_discrepancy}</div>}
      {notice && (
        <div className={`card notice-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>
          {notice.text}
        </div>
      )}

      <div className="detail-grid">
        <section className="card" aria-labelledby="original-heading">
          <h3 id="original-heading">Original document</h3>
          <a href={fileUrl} target="_blank" rel="noopener noreferrer">Open original</a>
          {doc.content_type === 'application/pdf' ? (
            <iframe src={fileUrl} title="Original document preview" className="doc-preview" />
          ) : (
            <img src={fileUrl} alt="Original document" className="doc-preview-img" />
          )}
          <div className="muted small">{doc.content_type} · {(doc.file_size / 1024).toFixed(1)} KB · SHA-256 {doc.sha256_hash.slice(0, 12)}…</div>
        </section>

        <div>
          <section className="card" aria-labelledby="details-heading">
            <div className="row-between">
              <h3 id="details-heading">{isBill ? 'Bill details' : 'Document details'}</h3>
              {!editing && can('update_fields') && (
                <button className="btn-secondary" onClick={() => setEditing(true)}>Edit details</button>
              )}
            </div>

            {editing ? (
              <form className="stack" onSubmit={(e) => { e.preventDefault(); handleSave(); }}>
                <label>Document type
                  <select value={form.document_type} onChange={(e) => setForm({ ...form, document_type: e.target.value })}>
                    {Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                  </select>
                </label>
                <label>Vendor name<input value={form.vendor_name} onChange={(e) => setForm({ ...form, vendor_name: e.target.value })} /></label>
                <label>Invoice number<input value={form.invoice_number} onChange={(e) => setForm({ ...form, invoice_number: e.target.value })} /></label>
                <div className="two-col">
                  <label>Invoice date<input type="date" value={form.invoice_date} onChange={(e) => setForm({ ...form, invoice_date: e.target.value })} /></label>
                  <label>Due date<input type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} /></label>
                </div>
                <div className="two-col">
                  <label>Total amount<input inputMode="decimal" value={form.total} onChange={(e) => setForm({ ...form, total: e.target.value })} /></label>
                  <label>Currency
                    <select value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })}>
                      <option value="">Unknown</option>
                      <option value="USD">USD</option><option value="EUR">EUR</option><option value="GBP">GBP</option><option value="CAD">CAD</option>
                    </select>
                  </label>
                </div>
                <label>Property
                  <select value={form.property_id} onChange={(e) => setForm({ ...form, property_id: e.target.value })}>
                    <option value="">Select property…</option>
                    {properties.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </label>
                {qbData ? (
                  <>
                    <label>QuickBooks vendor
                      <select value={form.qb_vendor_id} onChange={(e) => setForm({ ...form, qb_vendor_id: e.target.value })}>
                        <option value="">Select vendor…</option>
                        {qbData.vendors.map((v) => <option key={v.id} value={v.id}>{v.display_name}</option>)}
                      </select>
                    </label>
                    <label>Expense account
                      <select value={form.qb_account_id} onChange={(e) => setForm({ ...form, qb_account_id: e.target.value })}>
                        <option value="">Select account…</option>
                        {qbData.accounts.map((a) => <option key={a.id} value={a.id}>{a.fully_qualified_name}</option>)}
                      </select>
                    </label>
                    {qbData.classes.length > 0 && (
                      <label>Class (optional)
                        <select value={form.qb_class_id} onChange={(e) => setForm({ ...form, qb_class_id: e.target.value })}>
                          <option value="">None</option>
                          {qbData.classes.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                        </select>
                      </label>
                    )}
                  </>
                ) : (
                  <p className="muted small">QuickBooks vendors and accounts are unavailable: {qbDataError || 'loading…'}</p>
                )}
                <div className="row">
                  <button type="submit" className="btn-primary" disabled={busy === 'save'}>
                    {busy === 'save' ? 'Saving…' : 'Save correction'}
                  </button>
                  <button type="button" className="btn-secondary" onClick={() => { setEditing(false); load(); }} disabled={busy === 'save'}>Cancel</button>
                </div>
              </form>
            ) : (
              <dl className="facts">
                <dt>Type</dt><dd>{categories[category] || category}
                  {doc.extracted_document_type && <span className="muted small"> (read as “{doc.extracted_document_type}”)</span>}
                </dd>
                <dt>Vendor</dt><dd>{value(doc.effective_vendor_name, 'vendor_name')}</dd>
                <dt>Invoice #</dt><dd>{value(doc.effective_invoice_number, 'invoice_number')}</dd>
                <dt>Invoice date</dt><dd>{value(doc.effective_invoice_date, 'invoice_date')}</dd>
                <dt>Due date</dt><dd>{value(doc.effective_due_date)}</dd>
                <dt>Total</dt><dd>{value(doc.effective_total, 'total')}</dd>
                <dt>Currency</dt><dd>{value(doc.effective_currency, 'currency')}</dd>
                <dt>Service address</dt><dd>{value(doc.extracted_service_address)}</dd>
                <dt>Property</dt><dd>{value(doc.property_name, 'property')}</dd>
                <dt>QB vendor</dt><dd>{value(doc.qb_vendor_id ? vendorName : null, 'qb_vendor')}</dd>
                <dt>Account</dt><dd>{value(doc.qb_account_id ? accountName : null, 'qb_account')}</dd>
              </dl>
            )}
            {doc.extraction_uncertain_fields.length > 0 && (
              <p className="muted small">The reader was unsure about: {doc.extraction_uncertain_fields.join(', ')}</p>
            )}
            {can('retry_extraction') && (
              <button className="btn-secondary" onClick={() => run('extract', async () => { await retryExtraction(id); })} disabled={busy === 'extract'}>
                {busy === 'extract' ? 'Reading…' : 'Retry reading'}
              </button>
            )}
          </section>

          <section className="card" aria-labelledby="qb-heading">
            <h3 id="qb-heading">QuickBooks</h3>
            {!isBill && !doc.qb_bill_id && (
              <p className="small">Filed as {(categories[category] || category).toLowerCase()}. It is not a bill, so nothing
                is sent to QuickBooks. If it does need paying, change the document type to “Bill / invoice”.</p>
            )}
            {company && (
              <p className={company.verified ? 'text-success small' : 'text-danger small'}>
                Company: {company.company_name || 'not connected'}
                {company.realm_id && ` (${company.realm_id})`} · {company.environment}
                {company.verified ? ' · verified test company' : ` · ${company.problem}`}
              </p>
            )}
            {doc.qb_bill_id && (
              <p>
                {doc.status === 'linked_existing' ? 'Linked existing bill' : 'Created bill'}{' '}
                <strong>{doc.qb_bill_doc_number || doc.qb_bill_id}</strong> (ID {doc.qb_bill_id})
                {doc.qb_attachment_id ? ` · PDF attached (${doc.qb_attachment_id})` : ''}
                {doc.qb_bill_url && <> · <a href={doc.qb_bill_url} target="_blank" rel="noopener noreferrer">Open bill in QuickBooks</a></>}
              </p>
            )}
            {doc.last_accounting_check_at && (
              <p className="muted small">
                Last duplicate check {fmt(doc.last_accounting_check_at)}: {doc.last_accounting_check_ok ? 'complete' : `failed — ${doc.last_accounting_check_error}`}
              </p>
            )}

            <div className="row wrap">
              {can('check_duplicates') && (
                <button className="btn-secondary" disabled={!!busy} onClick={() => run('check', async () => {
                  const r = await checkDuplicates(id);
                  return r.candidates_found ? { kind: 'info', text: 'Possible existing bill found. Review it below.' }
                    : { kind: 'success', text: 'No matching bill in QuickBooks.' };
                })}>
                  {busy === 'check' ? 'Checking…' : 'Check QuickBooks for existing bill'}
                </button>
              )}
              {can('approve') && !confirming && (
                <button className="btn-primary" disabled={!!busy} onClick={() => setConfirming(true)}>Review and approve</button>
              )}
              {can('reconcile') && (
                <button className="btn-primary" disabled={!!busy} onClick={() => run('reconcile', async () => {
                  const r = await reconcileDocument(id);
                  return { kind: r.status === 'recorded' ? 'success' : 'info', text: r.error || `Status: ${r.status.replace(/_/g, ' ')}` };
                })}>
                  {busy === 'reconcile' ? 'Checking QuickBooks…' : 'Check QuickBooks and resume'}
                </button>
              )}
              {can('retry_attachment') && (
                <button className="btn-primary" disabled={!!busy} onClick={() => run('attach', async () => {
                  const r = await retryAttachment(id);
                  return { kind: r.status === 'recorded' ? 'success' : 'error', text: r.error || 'Original attached.' };
                })}>
                  {busy === 'attach' ? 'Checking attachments…' : 'Retry attachment'}
                </button>
              )}
            </div>

            {confirming && (
              <div className="confirm-panel" role="region" aria-label="Confirm unpaid bill">
                <h4>Create this unpaid bill in QuickBooks?</h4>
                <dl className="facts">
                  <dt>Company</dt><dd>{company?.company_name} ({company?.realm_id})</dd>
                  <dt>Vendor</dt><dd>{vendorName}</dd>
                  <dt>Invoice #</dt><dd>{doc.effective_invoice_number}</dd>
                  <dt>Total</dt><dd>{doc.effective_currency} {String(doc.effective_total)}</dd>
                  <dt>Account</dt><dd>{accountName}</dd>
                  <dt>Property</dt><dd>{doc.property_name}</dd>
                  <dt>Class</dt><dd>{className || 'None'}</dd>
                </dl>
                <p className="small muted">This records an unpaid bill and attaches the original. It does not pay anything.</p>
                <div className="row">
                  <button className="btn-success" onClick={handleCreateBill} disabled={!!busy}>Create unpaid bill</button>
                  <button className="btn-secondary" onClick={() => setConfirming(false)}>Cancel</button>
                </div>
              </div>
            )}
            {busy === 'approve' && <p role="status">Creating bill…</p>}

            {openCandidates.length > 0 && (
              <div role="region" aria-label="Possible matching bills">
                <h4>Possible matching bills in QuickBooks</h4>
                {openCandidates.map((c) => (
                  <div key={c.id} className="candidate" aria-label={`Match ${c.qb_doc_number || c.qb_bill_id}`} role="group">
                    <div><strong>Bill {c.qb_doc_number || c.qb_bill_id}</strong> · {c.qb_vendor_name} · {c.qb_currency} {String(c.qb_total ?? '')} · {c.qb_txn_date}</div>
                    <div className="small">{c.match_reason}{c.seen_count === 1 && resolvedCandidates.length > 0 ? ' · newly found' : ''}</div>
                    {c.match_details && (
                      <table className="compare">
                        <thead><tr><th>Field</th><th>This bill</th><th>QuickBooks</th><th>Match</th></tr></thead>
                        <tbody>
                          {Object.entries(c.match_details).map(([field, cmp]) => (
                            <tr key={field}>
                              <td>{field.replace(/_/g, ' ')}</td><td>{cmp.local ?? '—'}</td>
                              <td>{cmp.remote_name || cmp.remote || '—'}</td><td>{cmp.match ? 'yes' : 'no'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    <div className="row wrap">
                      <button className="btn-primary" disabled={!!busy} onClick={() => run(`link-${c.id}`, async () => {
                        await resolveDuplicate(id, c.id, 'linked', 'Owner confirmed same invoice');
                        return { kind: 'success', text: `Linked to existing bill ${c.qb_doc_number || c.qb_bill_id}. No new bill was created.` };
                      })}>Link to this bill</button>
                      <button className="btn-secondary" disabled={!!busy} onClick={() => { setDismissing(c.id); setDismissReason(''); }}>Not the same bill</button>
                    </div>
                    {dismissing === c.id && (
                      <form className="stack" onSubmit={(e) => {
                        e.preventDefault();
                        run(`dismiss-${c.id}`, async () => {
                          await resolveDuplicate(id, c.id, 'dismissed', dismissReason);
                          setDismissing(null);
                          return { kind: 'info', text: 'Match dismissed. QuickBooks will be checked again before posting.' };
                        });
                      }}>
                        <label>Why is this a different bill?<input value={dismissReason} onChange={(e) => setDismissReason(e.target.value)} required /></label>
                        <button type="submit" className="btn-secondary" disabled={!dismissReason.trim()}>Dismiss match</button>
                      </form>
                    )}
                  </div>
                ))}
              </div>
            )}
            {resolvedCandidates.length > 0 && (
              <ul className="small muted" aria-label="Resolved matches">
                {resolvedCandidates.map((c) => (
                  <li key={c.id}>
                    Bill {c.qb_doc_number || c.qb_bill_id}: {c.resolution === 'dismissed' ? `dismissed by ${c.resolved_by} — “${c.resolution_reason}”` : 'linked'}
                    {c.resolution === 'dismissed' && c.seen_count > 1 ? ' (still present in QuickBooks)' : ''}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="card" aria-labelledby="arrivals-heading">
            <h3 id="arrivals-heading">Arrivals ({doc.arrivals.length})</h3>
            <ul className="plain">
              {doc.arrivals.map((a) => (
                <li key={a.id} className="arrival">
                  <strong>{a.method === 'email' ? 'Email' : 'Upload'}</strong>
                  {a.original_filename && ` — ${a.original_filename}`}
                  <div className="muted small">
                    {fmt(a.arrived_at)}
                    {a.email_from && ` · from ${a.email_from}`}
                    {a.email_subject && ` · “${a.email_subject}”`}
                    {a.uploaded_by && ` · by ${a.uploaded_by}`}
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="card" aria-labelledby="history-heading">
            <h3 id="history-heading">History</h3>
            <ol className="plain history">
              {history.map((e) => (
                <li key={e.id}>
                  <div>{e.summary}</div>
                  <div className="muted small">
                    {fmt(e.occurred_at)} · {e.actor_kind === 'owner' ? `by ${e.actor}` : 'system'}
                    {e.links.map((l) => (
                      <span key={l.href + l.label}> · <a href={l.href} target="_blank" rel="noopener noreferrer">{l.label}</a></span>
                    ))}
                  </div>
                </li>
              ))}
            </ol>
          </section>
        </div>
      </div>
      <p className="muted small">Fields still unknown: {doc.missing_fields.length ? doc.missing_fields.map((f) => FIELD_LABELS[f] || f).join(', ') : 'none'}</p>
    </div>
  );
}
