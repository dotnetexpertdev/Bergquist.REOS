import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ResetDemo from './ResetDemo';
import * as api from '../api';

vi.mock('../api');

const preview: api.ResetDemoPreview = {
  confirm_word: 'RESET', documents: 3, arrivals: 4, history_events: 20, stored_files: 5, todoist_tasks: 0,
  quickbooks: {
    available: true, problem: null, company_name: 'Sandbox Co',
    bills: [{ id: '101', doc_number: 'MPL-1', vendor_name: 'Metro Power', total: '142.5', txn_date: '2026-10-15' }],
  },
};
const done: api.ResetDemoResult = {
  success: true, documents_deleted: 3, files_deleted: 5, files_left: 0, todoist_tasks_closed: 0,
  quickbooks: { bills_deleted: 1, attachments_deleted: 1, skipped: false }, backup: 'backup-1.zip',
};

describe('ResetDemo', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getResetDemoPreview).mockResolvedValue(preview);
    vi.mocked(api.resetDemo).mockResolvedValue(done);
  });

  it('lists what will be deleted and stays disabled until the word is typed', async () => {
    render(<ResetDemo onClose={() => undefined} />);
    expect(await screen.findByText(/3 document\(s\), 4 arrival\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/Metro Power · bill MPL-1/)).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Delete demo data' });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Type RESET to confirm'), 'RESET');
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(api.resetDemo).toHaveBeenCalledWith('RESET', true));
    expect(await screen.findByRole('status')).toHaveTextContent('1 QuickBooks bill(s)');
  });

  it('can leave QuickBooks alone, and shows a failure without closing', async () => {
    vi.mocked(api.resetDemo).mockRejectedValue(new Error('QuickBooks did not delete 1 bill(s)'));
    render(<ResetDemo onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('checkbox'));
    await userEvent.type(screen.getByLabelText('Type RESET to confirm'), 'RESET');
    await userEvent.click(screen.getByRole('button', { name: 'Delete demo data' }));
    await waitFor(() => expect(api.resetDemo).toHaveBeenCalledWith('RESET', false));
    expect(await screen.findByRole('alert')).toHaveTextContent('did not delete');
  });

  it('explains when QuickBooks cannot be cleaned', async () => {
    vi.mocked(api.getResetDemoPreview).mockResolvedValue({
      ...preview, quickbooks: { available: false, problem: 'QuickBooks is not connected', company_name: null, bills: [] },
    });
    render(<ResetDemo onClose={() => undefined} />);
    expect(await screen.findByText(/QuickBooks will not be cleaned: QuickBooks is not connected/)).toBeInTheDocument();
  });
});
