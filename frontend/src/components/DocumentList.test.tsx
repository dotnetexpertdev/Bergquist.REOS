import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({
  getDocuments: vi.fn(), getNeedsReview: vi.fn(), getGmailStatus: vi.fn(), importGmailMessages: vi.fn(),
  uploadDocument: vi.fn(), DOCUMENTS_CHANGED: 'documents-changed',
}));

import * as api from '../api';
import DocumentList from './DocumentList';

const m = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ json: async () => ({ max_upload_size_mb: 25 }) }));
  m.getDocuments.mockResolvedValue([{ id: 'd1', status: 'needs_review', created_at: '2026-11-12T10:00:00', revision: 3,
    vendor_name: 'City Water Dept', total: '61.20', currency: 'USD', arrival_count: 2, missing_fields: ['property'],
    pending_question: 'Which property is this for?' }]);
  m.getGmailStatus.mockResolvedValue({ configured: true, connected: true });
});

const renderList = () => render(<MemoryRouter><DocumentList /></MemoryRouter>);

describe('DocumentList', () => {
  it('lists the outstanding question and arrival count', async () => {
    renderList();
    const list = await screen.findByRole('list', { name: 'Documents' });
    expect(within(list).getByText('Which property is this for?')).toBeInTheDocument();
    expect(within(list).getByText(/2 arrivals/)).toBeInTheDocument();
    expect(screen.getByText(/Supported formats: PDF, JPEG, PNG · maximum 25 MB/)).toBeInTheDocument();
  });

  it('retries an interrupted upload with the same attempt id and links to the document', async () => {
    const user = userEvent.setup();
    m.uploadDocument.mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce({ success: true, document_id: 'd7', message: 'Document uploaded.' });
    renderList();
    const file = new File(['%PDF-1.4'], 'bill.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText('Upload bill'), file);
    expect(await screen.findByRole('alert')).toHaveTextContent('Network error');
    await user.click(screen.getByRole('button', { name: 'Retry upload' }));
    const [firstFile, firstAttempt] = m.uploadDocument.mock.calls[0];
    const [, secondAttempt] = m.uploadDocument.mock.calls[1];
    expect(firstFile.name).toBe('bill.pdf');
    expect(secondAttempt).toBe(firstAttempt);
    expect(await screen.findByRole('link', { name: 'Open document' })).toHaveAttribute('href', '/documents/d7');
  });

  it('uploads every file dropped on the drop area, one after another', async () => {
    m.uploadDocument.mockResolvedValueOnce({ success: true, document_id: 'd8', message: 'Document uploaded.' })
      .mockResolvedValueOnce({ success: true, document_id: 'd9', message: 'Document uploaded.' });
    renderList();
    const zone = await screen.findByLabelText('Drop files here');
    const files = [new File(['%PDF-1.4 a'], 'a.pdf', { type: 'application/pdf' }),
      new File(['%PDF-1.4 b'], 'b.pdf', { type: 'application/pdf' })];
    fireEvent.dragOver(zone, { dataTransfer: { files } });
    expect(zone).toHaveTextContent('Release to upload');
    fireEvent.drop(zone, { dataTransfer: { files } });
    expect(await screen.findByText(/2 files received/)).toBeInTheDocument();
    expect(m.uploadDocument.mock.calls.map((c) => c[0].name)).toEqual(['a.pdf', 'b.pdf']);
    expect(m.uploadDocument.mock.calls[0][1]).not.toBe(m.uploadDocument.mock.calls[1][1]); // separate attempt IDs
    expect(screen.getByRole('link', { name: 'Open document' })).toHaveAttribute('href', '/documents/d9');
  });

  it('imports from the inbox and shows counts with a retry for failures', async () => {
    const user = userEvent.setup();
    m.importGmailMessages.mockResolvedValue({ success: false, messages_found: 2, attachments_processed: 2, imported: 1,
      already_processed: 0, skipped: 0, failed: 1, results: [
        { message_id: 'm2', attachment_filename: 'b.pdf', success: false, is_duplicate: false, error: 'Temporary Gmail error' }] });
    renderList();
    await user.click(await screen.findByRole('button', { name: 'Import From Inbox' }));
    const results = await screen.findByRole('status', { name: 'Import results' });
    expect(results).toHaveTextContent('Imported: 1');
    expect(results).toHaveTextContent('b.pdf: Temporary Gmail error');
    await user.click(within(results).getByRole('button', { name: 'Retry import' }));
    expect(m.importGmailMessages).toHaveBeenCalledTimes(2);
  });
});
