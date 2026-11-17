import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  authorizeGmail, authorizeQuickBooks, createAccountMapping, createProperty, createVendorMapping,
  deleteAccountMapping, deleteVendorMapping, disconnectGmail, disconnectQuickBooks, getAccountMappings,
  getIntegrationsStatus, getProperties, getQBReferenceData, getQBStatus, getVendorMappings,
} from '../api';
import type {
  AccountMapping, AllIntegrationsStatus, Property, QBConnectionStatus, QBReferenceData, VendorMapping,
} from '../types';

const detail = (status: AllIntegrationsStatus['gmail'] | undefined, key: string) =>
  (status?.details as Record<string, string | undefined> | null | undefined)?.[key];

export default function Settings() {
  const [params] = useSearchParams();
  const [integrations, setIntegrations] = useState<AllIntegrationsStatus | null>(null);
  const [qbStatus, setQBStatus] = useState<QBConnectionStatus | null>(null);
  const [properties, setProperties] = useState<Property[]>([]);
  const [qbRefData, setQbRefData] = useState<QBReferenceData | null>(null);
  const [vendorMappings, setVendorMappings] = useState<VendorMapping[]>([]);
  const [accountMappings, setAccountMappings] = useState<AccountMapping[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [confirmDisconnect, setConfirmDisconnect] = useState<'' | 'gmail' | 'quickbooks'>('');

  const [newPropertyName, setNewPropertyName] = useState('');
  const [newPropertyAddress, setNewPropertyAddress] = useState('');
  const [newVendorExtracted, setNewVendorExtracted] = useState('');
  const [newVendorQBId, setNewVendorQBId] = useState('');
  const [newAccountKeyword, setNewAccountKeyword] = useState('');
  const [newAccountQBId, setNewAccountQBId] = useState('');

  const oauthError = params.get('qb_error') || params.get('gmail_error');
  const oauthOk = params.get('qb_connected') ? 'QuickBooks connected.' : params.get('gmail_connected') ? 'Gmail connected.' : '';

  const refresh = async () => {
    try {
      const [intStatus, qb, props, vm, am] = await Promise.all([
        getIntegrationsStatus(), getQBStatus(), getProperties(),
        getVendorMappings().catch(() => []), getAccountMappings().catch(() => []),
      ]);
      setIntegrations(intStatus);
      setQBStatus(qb);
      setProperties(props);
      setVendorMappings(vm);
      setAccountMappings(am);
      if (qb.connected) setQbRefData(await getQBReferenceData().catch(() => null));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const act = async (fn: () => Promise<unknown>) => {
    setError('');
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed');
    }
    await refresh();
  };

  if (loading) {
    return <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}><div className="spinner" aria-label="Loading" /></div>;
  }

  return (
    <div className="settings">
      <h2>Settings</h2>
      {oauthError && <div className="card notice-error" role="alert">Connection problem: {oauthError.replace(/_/g, ' ')}
        {params.get('expected') && ` (expected company ${params.get('expected')}, got ${params.get('got')})`}</div>}
      {oauthOk && <div className="card notice-success" role="status">{oauthOk}</div>}
      {error && <div className="card notice-error" role="alert">{error}</div>}

      <section className="card" aria-labelledby="gmail-h">
        <h3 id="gmail-h">Gmail</h3>
        <p className="small muted">Redirect URI to register in Google Cloud: <code>{detail(integrations?.gmail, 'redirect_uri') || '—'}</code></p>
        {integrations?.gmail.connected ? (
          <>
            <p className="text-success">Connected to {detail(integrations.gmail, 'account')} · label “{detail(integrations.gmail, 'label')}”</p>
            {confirmDisconnect === 'gmail' ? (
              <div className="row"><button className="btn-danger" onClick={() => act(disconnectGmail).then(() => setConfirmDisconnect(''))}>Confirm disconnect Gmail</button>
                <button className="btn-secondary" onClick={() => setConfirmDisconnect('')}>Keep connected</button></div>
            ) : <button className="btn-danger" onClick={() => setConfirmDisconnect('gmail')}>Disconnect Gmail</button>}
          </>
        ) : !integrations?.gmail.configured ? (
          <p className="muted">Not configured. Add GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET to the server .env file.</p>
        ) : (
          <>
            {integrations.gmail.error && <p className="text-danger">{integrations.gmail.error}</p>}
            <button className="btn-primary" onClick={() => act(authorizeGmail)}>Connect Gmail (read-only)</button>
          </>
        )}
      </section>

      <section className="card" aria-labelledby="qb-h">
        <h3 id="qb-h">QuickBooks</h3>
        <p className="small muted">Redirect URI to register with Intuit: <code>{qbStatus?.redirect_uri || '—'}</code></p>
        <p className="small muted">Environment: {qbStatus?.environment} · configured test company: {qbStatus?.expected_realm_id || 'not set (writes blocked)'}</p>
        {qbStatus?.connected ? (
          <>
            <p className={qbStatus.verified ? 'text-success' : 'text-danger'}>
              Connected to {qbStatus.company_name || 'unknown company'} ({qbStatus.realm_id}) — {qbStatus.verified ? 'verified test company' : qbStatus.last_error}
            </p>
            {confirmDisconnect === 'quickbooks' ? (
              <div className="row"><button className="btn-danger" onClick={() => act(disconnectQuickBooks).then(() => setConfirmDisconnect(''))}>Confirm disconnect QuickBooks</button>
                <button className="btn-secondary" onClick={() => setConfirmDisconnect('')}>Keep connected</button></div>
            ) : <button className="btn-danger" onClick={() => setConfirmDisconnect('quickbooks')}>Disconnect QuickBooks</button>}
          </>
        ) : !qbStatus?.configured ? (
          <p className="muted">Not configured. Add QUICKBOOKS_CLIENT_ID and QUICKBOOKS_CLIENT_SECRET to the server .env file.</p>
        ) : (
          <>
            {qbStatus.last_error && <p className="text-danger">{qbStatus.last_error}</p>}
            <button className="btn-primary" onClick={() => act(authorizeQuickBooks)}>Connect QuickBooks</button>
          </>
        )}
      </section>

      <section className="card" aria-labelledby="ai-h">
        <h3 id="ai-h">OpenAI</h3>
        {integrations?.openai.configured ? (
          <p className="text-success">Configured on the server · extraction {detail(integrations.openai, 'extraction_model')} · transcription {detail(integrations.openai, 'transcription_model')}</p>
        ) : (
          <p className="muted">Not configured. Set OPENAI_API_KEY in the server .env file (never in the browser). Bills can still be entered by hand.</p>
        )}
      </section>

      <section className="card" aria-labelledby="todo-h">
        <h3 id="todo-h">Todoist (optional)</h3>
        <p className={integrations?.todoist.configured ? 'text-success' : 'muted'}>
          {integrations?.todoist.configured ? 'Enabled' : integrations?.todoist.error || 'Disabled (TODOIST_ENABLED=false)'}
        </p>
      </section>

      <section className="card" aria-labelledby="props-h">
        <h3 id="props-h">Properties</h3>
        <ul className="plain">
          {properties.map((p) => <li key={p.id}><strong>{p.name}</strong>{p.address && <span className="muted small"> — {p.address}</span>}</li>)}
        </ul>
        <form className="stack" onSubmit={(e) => {
          e.preventDefault();
          act(async () => { await createProperty(newPropertyName.trim(), newPropertyAddress.trim() || undefined); setNewPropertyName(''); setNewPropertyAddress(''); });
        }}>
          <label>Property name<input value={newPropertyName} onChange={(e) => setNewPropertyName(e.target.value)} required /></label>
          <label>Address (used to match service addresses)<input value={newPropertyAddress} onChange={(e) => setNewPropertyAddress(e.target.value)} /></label>
          <button type="submit" className="btn-primary" disabled={!newPropertyName.trim()}>Add property</button>
        </form>
      </section>

      <section className="card" aria-labelledby="vm-h">
        <h3 id="vm-h">Vendor mappings</h3>
        <ul className="plain">
          {vendorMappings.map((m) => (
            <li key={m.id} className="row-between">{m.extracted_name} → {m.qb_vendor_name}
              <button className="btn-danger small" onClick={() => act(() => deleteVendorMapping(m.id))}>Delete</button></li>
          ))}
        </ul>
        {qbRefData ? (
          <form className="stack" onSubmit={(e) => {
            e.preventDefault();
            const v = qbRefData.vendors.find((x) => x.id === newVendorQBId);
            if (v) act(async () => { await createVendorMapping(newVendorExtracted.trim(), v.id, v.display_name); setNewVendorExtracted(''); setNewVendorQBId(''); });
          }}>
            <label>Vendor name as printed on bills<input value={newVendorExtracted} onChange={(e) => setNewVendorExtracted(e.target.value)} required /></label>
            <label>QuickBooks vendor
              <select value={newVendorQBId} onChange={(e) => setNewVendorQBId(e.target.value)} required>
                <option value="">Select…</option>
                {qbRefData.vendors.map((v) => <option key={v.id} value={v.id}>{v.display_name}</option>)}
              </select>
            </label>
            <button type="submit" className="btn-primary" disabled={!newVendorExtracted.trim() || !newVendorQBId}>Add vendor mapping</button>
          </form>
        ) : <p className="muted">Connect QuickBooks to manage vendor mappings.</p>}
      </section>

      <section className="card" aria-labelledby="am-h">
        <h3 id="am-h">Expense account mappings</h3>
        <ul className="plain">
          {accountMappings.map((m) => (
            <li key={m.id} className="row-between">“{m.category}” → {m.qb_account_name}
              <button className="btn-danger small" onClick={() => act(() => deleteAccountMapping(m.id))}>Delete</button></li>
          ))}
        </ul>
        {qbRefData ? (
          <form className="stack" onSubmit={(e) => {
            e.preventDefault();
            const a = qbRefData.accounts.find((x) => x.id === newAccountQBId);
            if (a) act(async () => { await createAccountMapping(newAccountKeyword.trim(), a.id, a.name); setNewAccountKeyword(''); setNewAccountQBId(''); });
          }}>
            <label>Keyword in vendor name or bill type (e.g. “utility”, “electric”)<input value={newAccountKeyword} onChange={(e) => setNewAccountKeyword(e.target.value)} required /></label>
            <label>Expense account
              <select value={newAccountQBId} onChange={(e) => setNewAccountQBId(e.target.value)} required>
                <option value="">Select…</option>
                {qbRefData.accounts.map((a) => <option key={a.id} value={a.id}>{a.fully_qualified_name}</option>)}
              </select>
            </label>
            <button type="submit" className="btn-primary" disabled={!newAccountKeyword.trim() || !newAccountQBId}>Add account mapping</button>
          </form>
        ) : <p className="muted">Connect QuickBooks to manage account mappings.</p>}
      </section>
    </div>
  );
}
