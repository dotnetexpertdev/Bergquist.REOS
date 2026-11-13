import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocumentDetail as Doc, DuplicateCandidate } from '../types';

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error { status = 0; retryable = false; },
  getDocument: vi.fn(), getDocumentHistory: vi.fn(), getDuplicateCandidates: vi.fn(), getProperties: vi.fn(),
  getQBReferenceData: vi.fn(), updateDocument: vi.fn(), approveDocument: vi.fn(), checkDuplicates: vi.fn(),
  resolveDuplicate: vi.fn(), reconcileDocument: vi.fn(), retryAttachment: vi.fn(), retryExtraction: vi.fn(),
  resolveCopy: vi.fn(),
}));

import * as api from '../api';
import DocumentDetail from './DocumentDetail';

const m = vi.mocked(api);

const company = { connected: true, company_name: 'Sandbox Test Company', realm_id: 'TEST-REALM',
  expected_realm_id: 'TEST-REALM', environment: 'sandbox', verified: true, problem: null };

function makeDoc(over: Partial<Doc> = {}): Doc {
  return {
    id: 'd1', sha256_hash: 'abcdef0123456789', content_type: 'application/pdf', file_size: 2048,
    status: 'ready_for_approval', revision: 7, created_at: '2026-11-12T10:00:00',
    extraction_uncertain_fields: [], effective_vendor_name: 'Metro Power & Light',
    effective_invoice_number: 'MPL-2026-0415', effective_invoice_date: '2026-10-15', effective_total: '142.50',
    effective_currency: 'USD', property_id: 'p1', property_name: 'Demo Property', qb_vendor_id: '56',
    qb_account_id: '7', arrivals: [
      { id: 'a1', arrived_at: '2026-11-12T10:00:00', method: 'email', original_filename: 'statement.pdf',
        email_from: 'billing@metropower.example' },
    ],
    available_actions: ['view_original', 'update_fields', 'check_duplicates', 'approve'],
    missing_fields: [], company, ...over,
  };
}

function renderAt(doc: Doc, candidates: DuplicateCandidate[] = []) {
  m.getDocument.mockResolvedValue(doc);
  m.getDocumentHistory.mockResolvedValue([
    { id: 'e1', event_type: 'arrival_recorded', occurred_at: '2026-11-12T10:00:00', actor: 'system',
      actor_kind: 'system', summary: 'Arrived by email from billing@metropower.example as statement.pdf',
      links: [{ label: 'Source email', href: 'https://mail.google.com/mail/u/0/#all/msg-A' }] },
  ]);
  m.getDuplicateCandidates.mockResolvedValue(candidates);
  m.getProperties.mockResolvedValue([{ id: 'p1', name: 'Demo Property' }, { id: 'p2', name: 'Oak Street Duplex' }]);
  m.getQBReferenceData.mockResolvedValue({
    vendors: [{ id: '56', name: 'Metro Power & Light', display_name: 'Metro Power & Light' }],
    accounts: [{ id: '7', name: 'Utilities', account_type: 'Expense', fully_qualified_name: 'Utilities' }],
    classes: [],
  });
  return render(
    <MemoryRouter initialEntries={['/documents/d1']}>
      <Routes><Route path="/documents/:id" element={<DocumentDetail />} /></Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('DocumentDetail review and approval', () => {
  it('shows the original link, arrivals and history with source links', async () => {
    renderAt(makeDoc());
    expect(await screen.findByRole('link', { name: 'Open original' })).toHaveAttribute('href', '/api/documents/d1/file');
    const history = screen.getByRole('region', { name: 'History' });
    expect(within(history).getByRole('link', { name: 'Source email' })).toHaveAttribute('href', expect.stringContaining('msg-A'));
    expect(screen.getByRole('heading', { name: 'Arrivals (1)' })).toBeInTheDocument();
  });

  it('requires the explicit Create unpaid bill action and shows what will be posted', async () => {
    const user = userEvent.setup();
    m.approveDocument.mockResolvedValue({ success: true, document_id: 'd1', status: 'recorded', qb_bill_id: '101',
      qb_bill_doc_number: 'MPL-2026-0415', requires_retry: false });
    renderAt(makeDoc());
    await user.click(await screen.findByRole('button', { name: 'Review and approve' }));
    expect(m.approveDocument).not.toHaveBeenCalled();
    const panel = screen.getByRole('region', { name: 'Confirm unpaid bill' });
    for (const text of ['Sandbox Test Company (TEST-REALM)', 'Metro Power & Light', 'MPL-2026-0415', 'USD 142.50', 'Utilities', 'Demo Property']) {
      expect(within(panel).getByText(text)).toBeInTheDocument();
    }
    await user.click(within(panel).getByRole('button', { name: 'Create unpaid bill' }));
    expect(m.approveDocument).toHaveBeenCalledTimes(1);
    expect(m.approveDocument).toHaveBeenCalledWith('d1', 7);
    expect(await screen.findByText(/Unpaid bill MPL-2026-0415 created/)).toBeInTheDocument();
  });

  it('cancelling the confirmation sends nothing', async () => {
    const user = userEvent.setup();
    renderAt(makeDoc());
    await user.click(await screen.findByRole('button', { name: 'Review and approve' }));
    await user.click(within(screen.getByRole('region', { name: 'Confirm unpaid bill' })).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('region', { name: 'Confirm unpaid bill' })).not.toBeInTheDocument();
    expect(m.approveDocument).not.toHaveBeenCalled();
  });

  it('shows unknown values and keeps approval unavailable', async () => {
    renderAt(makeDoc({ status: 'needs_review', property_id: null, property_name: null, effective_currency: null,
      missing_fields: ['currency', 'property'], pending_question: 'What currency is this bill in?',
      available_actions: ['view_original', 'update_fields'] }));
    const details = await screen.findByRole('region', { name: 'Bill details' });
    expect(within(details).getAllByText('Unknown — needs review')).toHaveLength(2);
    expect(screen.getByRole('status', { name: 'Question for you' })).toHaveTextContent('What currency is this bill in?');
    expect(screen.queryByRole('button', { name: 'Review and approve' })).not.toBeInTheDocument();
  });

  it('saves a correction with the current revision and never approves', async () => {
    const user = userEvent.setup();
    m.updateDocument.mockResolvedValue({ success: true, revision: 8, status: 'ready_for_approval' });
    renderAt(makeDoc());
    await user.click(await screen.findByRole('button', { name: 'Edit details' }));
    await user.selectOptions(screen.getByLabelText('Property'), 'p2');
    await user.click(screen.getByRole('button', { name: 'Save correction' }));
    expect(m.updateDocument).toHaveBeenCalledWith('d1', expect.objectContaining({ revision: 7, property_id: 'p2', source: 'typed' }));
    expect(m.approveDocument).not.toHaveBeenCalled();
    expect(await screen.findByText('Correction saved. Nothing was sent to QuickBooks.')).toBeInTheDocument();
  });

  it('reports a stale-tab conflict from the server', async () => {
    const user = userEvent.setup();
    m.updateDocument.mockRejectedValue(new Error('This bill was changed elsewhere (current revision 9, yours 7).'));
    renderAt(makeDoc());
    await user.click(await screen.findByRole('button', { name: 'Edit details' }));
    await user.click(screen.getByRole('button', { name: 'Save correction' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('changed elsewhere');
  });
});

describe('DocumentDetail duplicate matches and recovery', () => {
  const candidate: DuplicateCandidate = {
    id: 'c1', qb_bill_id: '77', qb_doc_number: 'NES-5531', qb_vendor_name: 'Northside Electric Supply',
    qb_total: '389.00', qb_currency: 'USD', qb_txn_date: '2026-09-30',
    match_reason: 'Same vendor and invoice number NES-5531; total, currency, date also match', seen_count: 1,
    resolved: false, match_details: { invoice_number: { local: 'NES-5531', remote: 'NES-5531', match: true },
      total: { local: '389.00', remote: '389.00', match: true } },
  };

  it('explains the match and links the existing bill', async () => {
    const user = userEvent.setup();
    m.resolveDuplicate.mockResolvedValue({ success: true, status: 'linked_existing' });
    renderAt(makeDoc({ status: 'duplicate_candidate', available_actions: ['link_existing', 'dismiss_duplicate'] }), [candidate]);
    const match = await screen.findByRole('group', { name: 'Match NES-5531' });
    expect(within(match).getByText(/Same vendor and invoice number NES-5531/)).toBeInTheDocument();
    expect(within(match).getByRole('table')).toHaveTextContent('invoice number');
    await user.click(within(match).getByRole('button', { name: 'Link to this bill' }));
    expect(m.resolveDuplicate).toHaveBeenCalledWith('d1', 'c1', 'linked', expect.any(String));
    expect(m.approveDocument).not.toHaveBeenCalled();
  });

  it('requires a reason to dismiss a match', async () => {
    const user = userEvent.setup();
    m.resolveDuplicate.mockResolvedValue({ success: true, status: 'ready_for_approval' });
    renderAt(makeDoc({ status: 'duplicate_candidate', available_actions: ['dismiss_duplicate'] }), [candidate]);
    const match = await screen.findByRole('group', { name: 'Match NES-5531' });
    await user.click(within(match).getByRole('button', { name: 'Not the same bill' }));
    const dismiss = within(match).getByRole('button', { name: 'Dismiss match' });
    expect(dismiss).toBeDisabled();
    await user.type(within(match).getByLabelText('Why is this a different bill?'), 'Different month');
    await user.click(dismiss);
    expect(m.resolveDuplicate).toHaveBeenCalledWith('d1', 'c1', 'dismissed', 'Different month');
  });

  it('offers reconcile for an unknown QuickBooks outcome', async () => {
    const user = userEvent.setup();
    m.reconcileDocument.mockResolvedValue({ success: true, document_id: 'd1', status: 'recorded', requires_retry: false });
    renderAt(makeDoc({ status: 'remote_outcome_unknown', available_actions: ['reconcile'],
      status_reason: 'QuickBooks did not confirm the bill.' }));
    await user.click(await screen.findByRole('button', { name: 'Check QuickBooks and resume' }));
    expect(m.reconcileDocument).toHaveBeenCalledWith('d1');
    expect(m.approveDocument).not.toHaveBeenCalled();
  });

  it('retries the attachment against the same bill', async () => {
    const user = userEvent.setup();
    m.retryAttachment.mockResolvedValue({ success: true, document_id: 'd1', status: 'attachment_pending',
      error: 'attachments could not be checked', requires_retry: true, retry_action: 'retry_attachment' });
    renderAt(makeDoc({ status: 'attachment_pending', qb_bill_id: '101', available_actions: ['retry_attachment'] }));
    await user.click(await screen.findByRole('button', { name: 'Retry attachment' }));
    expect(m.retryAttachment).toHaveBeenCalledWith('d1');
    expect(await screen.findByRole('alert')).toHaveTextContent('attachments could not be checked');
  });
});

describe('DocumentDetail classification and copies', () => {
  it('asks whether a possible copy is the same invoice and links it', async () => {
    const user = userEvent.setup();
    m.resolveCopy.mockResolvedValue({ success: true, revision: 8, status: 'linked_copy' });
    renderAt(makeDoc({
      status: 'possible_copy', available_actions: ['view_original', 'update_fields', 'resolve_copy'],
      copy_of: { id: 'd0', title: 'Metro Power & Light invoice MPL-2026-0415', status: 'recorded', total: '142.50',
        currency: 'USD', invoice_date: '2026-10-15', qb_bill_id: '101', qb_bill_doc_number: 'MPL-2026-0415',
        confirmed: false },
    }));
    const panel = await screen.findByRole('region', { name: 'Copy of a stored bill' });
    expect(within(panel).getByRole('link', { name: 'Metro Power & Light invoice MPL-2026-0415' }))
      .toHaveAttribute('href', '/documents/d0');
    expect(screen.queryByRole('button', { name: 'Review and approve' })).not.toBeInTheDocument();
    await user.click(within(panel).getByRole('button', { name: 'Same invoice: link as copy' }));
    expect(m.resolveCopy).toHaveBeenCalledWith('d1', 7, 'same');
    expect(await screen.findByText('Linked as a copy. No second bill will be created.')).toBeInTheDocument();
  });

  it('keeps it separate when the owner says it is a different invoice', async () => {
    const user = userEvent.setup();
    m.resolveCopy.mockResolvedValue({ success: true, revision: 8, status: 'ready_for_approval' });
    renderAt(makeDoc({
      status: 'possible_copy', available_actions: ['view_original', 'update_fields', 'resolve_copy'],
      copy_of: { id: 'd0', title: 'Metro Power & Light', status: 'needs_review', confirmed: false },
    }));
    const panel = await screen.findByRole('region', { name: 'Copy of a stored bill' });
    await user.click(within(panel).getByRole('button', { name: 'Different invoice' }));
    expect(m.resolveCopy).toHaveBeenCalledWith('d1', 7, 'different');
  });

  it('shows a filed document without accounting actions and lets the owner reclassify it', async () => {
    const user = userEvent.setup();
    m.updateDocument.mockResolvedValue({ success: true, revision: 8, status: 'needs_review' });
    renderAt(makeDoc({
      status: 'filed', document_category: 'receipt', extracted_document_type: 'receipt',
      available_actions: ['view_original', 'view_history', 'update_fields'],
    }));
    expect(await screen.findByRole('heading', { name: 'Document details' })).toBeInTheDocument();
    expect(screen.getByText(/It is not a bill, so nothing/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review and approve' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Edit details' }));
    await user.selectOptions(screen.getByLabelText('Document type'), 'bill');
    await user.click(screen.getByRole('button', { name: 'Save correction' }));
    expect(m.updateDocument).toHaveBeenCalledWith('d1', expect.objectContaining({ revision: 7, reviewed_document_type: 'bill' }));
  });

  it('does not send the document type when it is unchanged', async () => {
    const user = userEvent.setup();
    m.updateDocument.mockResolvedValue({ success: true, revision: 8, status: 'ready_for_approval' });
    renderAt(makeDoc({ document_category: 'bill' }));
    await user.click(await screen.findByRole('button', { name: 'Edit details' }));
    await user.click(screen.getByRole('button', { name: 'Save correction' }));
    expect(m.updateDocument.mock.calls[0][1].reviewed_document_type).toBeUndefined();
  });
});
