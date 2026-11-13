import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SearchResponse } from '../types';

vi.mock('../api', () => ({ search: vi.fn(), transcribe: vi.fn(), updateDocument: vi.fn() }));

import * as api from '../api';
import SearchBar from './SearchBar';

const m = vi.mocked(api);

const answer = (over: Partial<SearchResponse> = {}): SearchResponse => ({
  success: true, answer: 'The Metro Power & Light invoice MPL-2026-0415 bill, USD 142.50, for Demo Property.',
  documents: [{ id: 'd1', vendor_name: 'Metro Power & Light', invoice_number: 'MPL-2026-0415', total: '142.50',
    currency: 'USD', status: 'recorded', arrival_count: 2, qb_bill_id: '101',
    links: { record: '/documents/d1', original: '/api/documents/d1/file', quickbooks: 'https://qbo/bill/101' } }],
  selected_document_id: 'd1', needs_clarification: false,
  history: [{ id: 'e1', event_type: 'bill_created', occurred_at: '2026-11-12T10:00:00', actor: 'system',
    actor_kind: 'system', summary: 'Unpaid bill created in QuickBooks: bill MPL-2026-0415 (ID 101)', links: [] }],
  ...over,
});

// --- MediaRecorder / microphone fakes --------------------------------------
const track = { stop: vi.fn() };
class FakeRecorder {
  static isTypeSupported = vi.fn((t: string) => t === 'audio/webm');
  mimeType = 'audio/webm';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  start = vi.fn();
  stop = vi.fn(() => {
    this.ondataavailable?.({ data: new Blob(['x'.repeat(2000)], { type: 'audio/webm' }) });
    this.onstop?.();
  });
}

function renderBar() {
  return render(<MemoryRouter><SearchBar onClose={() => undefined} /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeRecorder;
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) } });
  (window as unknown as { speechSynthesis: unknown }).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  (globalThis as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = class { text: string; onend = null; onerror = null; constructor(t: string) { this.text = t; } };
});
afterEach(() => vi.restoreAllMocks());

describe('Assistant questions', () => {
  it('answers a typed question with history and source links', async () => {
    const user = userEvent.setup();
    m.search.mockResolvedValue(answer());
    renderBar();
    await user.type(screen.getByLabelText('Question'), 'What happened to the electricity bill?');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(m.search).toHaveBeenCalledWith('What happened to the electricity bill?', undefined, undefined);
    const region = await screen.findByRole('region', { name: 'Answer' });
    expect(region).toHaveTextContent('USD 142.50');
    const sources = screen.getByRole('region', { name: 'Sources' });
    expect(within(sources).getByRole('link', { name: 'Original' })).toHaveAttribute('href', '/api/documents/d1/file');
    expect(within(sources).getByRole('link', { name: /QuickBooks bill/ })).toBeInTheDocument();
    expect(within(sources).getByRole('list', { name: 'Supporting history' })).toHaveTextContent('Unpaid bill created');
  });

  it('uses the selected bill for a follow-up question', async () => {
    const user = userEvent.setup();
    m.search.mockResolvedValueOnce(answer()).mockResolvedValueOnce(answer({ answer: 'The bill is for Demo Property.' }));
    renderBar();
    const box = screen.getByLabelText('Question');
    await user.type(box, 'What happened to the electricity bill?');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    await screen.findByRole('region', { name: 'Answer' });
    await user.clear(box);
    await user.type(box, 'Which property was it for?');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(m.search).toHaveBeenLastCalledWith('Which property was it for?', 'd1', undefined);
  });

  it('asks the owner to choose when two bills fit, then follows the choice', async () => {
    const user = userEvent.setup();
    m.search.mockResolvedValueOnce(answer({ needs_clarification: true, selected_document_id: null,
      clarification_question: '2 bills could match. Which one do you mean?', pending_query: 'electricity bill',
      documents: [
        { id: 'd1', vendor_name: 'Metro Power & Light', invoice_number: 'A-1', total: '10', currency: 'USD', status: 'recorded', arrival_count: 1, links: {} },
        { id: 'd2', vendor_name: 'Metro Power & Light', invoice_number: 'B-2', total: '20', currency: 'USD', status: 'needs_review', arrival_count: 1, links: {} },
      ] })).mockResolvedValueOnce(answer({ selected_document_id: 'd2' }));
    renderBar();
    await user.type(screen.getByLabelText('Question'), 'electricity bill');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const choices = await screen.findByRole('group', { name: 'Choose a bill' });
    await user.click(within(choices).getByRole('button', { name: /#B-2/ }));
    expect(m.search).toHaveBeenLastCalledWith('electricity bill', undefined, 'd2');
  });

  it('plays and stops the answer with a synthetic voice label', async () => {
    const user = userEvent.setup();
    m.search.mockResolvedValue(answer());
    renderBar();
    await user.type(screen.getByLabelText('Question'), 'power bill');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const region = await screen.findByRole('region', { name: 'Answer' });
    expect(within(region).getByText('Synthetic voice')).toBeInTheDocument();
    await user.click(within(region).getByRole('button', { name: 'Play answer' }));
    expect(window.speechSynthesis.speak).toHaveBeenCalledTimes(1);
    await user.click(within(region).getByRole('button', { name: 'Stop playback' }));
    expect(window.speechSynthesis.cancel).toHaveBeenCalled();
  });
});

describe('Voice input', () => {
  it('records, transcribes into an editable box, and waits for Ask', async () => {
    const user = userEvent.setup();
    m.transcribe.mockResolvedValue({ success: true, transcript: 'What happened to the electricity bill' });
    m.search.mockResolvedValue(answer());
    renderBar();
    await user.click(screen.getByRole('button', { name: 'Start voice recording' }));
    expect(screen.getByRole('status')).toHaveTextContent('Recording…');
    await user.click(screen.getByRole('button', { name: 'Stop recording' }));
    await waitFor(() => expect(screen.getByLabelText('Question')).toHaveValue('What happened to the electricity bill'));
    expect(m.transcribe).toHaveBeenCalledTimes(1);
    expect(track.stop).toHaveBeenCalled(); // microphone released
    expect(m.search).not.toHaveBeenCalled(); // transcript is reviewed first
    await user.type(screen.getByLabelText('Question'), '?');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(m.search).toHaveBeenCalledWith('What happened to the electricity bill?', undefined, undefined);
  });

  it('cancelling a recording triggers no action', async () => {
    const user = userEvent.setup();
    renderBar();
    await user.click(screen.getByRole('button', { name: 'Start voice recording' }));
    await user.click(screen.getByRole('button', { name: 'Cancel recording' }));
    expect(m.transcribe).not.toHaveBeenCalled();
    expect(m.search).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Recording cancelled.');
  });

  it('keeps typing available when the microphone is denied', async () => {
    const user = userEvent.setup();
    (navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mockRejectedValue(new DOMException('denied', 'NotAllowedError'));
    m.search.mockResolvedValue(answer());
    renderBar();
    await user.click(screen.getByRole('button', { name: 'Start voice recording' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Microphone access was denied. You can type your question instead.');
    const box = screen.getByLabelText('Question');
    expect(box).toBeEnabled();
    await user.type(box, 'power bill');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(m.search).toHaveBeenCalled();
  });

  it('shows a retry after a transcription failure', async () => {
    const user = userEvent.setup();
    m.transcribe.mockResolvedValueOnce({ success: false, error: 'Transcription failed: timeout' })
      .mockResolvedValueOnce({ success: true, transcript: 'power bill' });
    renderBar();
    await user.click(screen.getByRole('button', { name: 'Start voice recording' }));
    await user.click(screen.getByRole('button', { name: 'Stop recording' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Transcription failed: timeout');
    expect(screen.getByLabelText('Question')).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Retry transcription' }));
    await waitFor(() => expect(screen.getByLabelText('Question')).toHaveValue('power bill'));
    expect(m.transcribe).toHaveBeenCalledTimes(2);
  });
});

describe('Spoken corrections', () => {
  const proposal = answer({
    answer: 'Proposed change to the City Water Dept bill: Property from empty to Oak Street Duplex. Nothing changes until you confirm.',
    proposed_edit: { document_id: 'd9', revision: 4, field: 'property', field_label: 'Property', update_key: 'property_id',
      current_value: null, proposed_value: 'Oak Street Duplex', proposed_raw: 'p2' },
  });

  it('applies a dictated correction only after confirmation, as a voice correction', async () => {
    const user = userEvent.setup();
    m.transcribe.mockResolvedValue({ success: true, transcript: 'Change the property on this bill to Oak Street' });
    m.search.mockResolvedValue(proposal);
    m.updateDocument.mockResolvedValue({ success: true, revision: 5, status: 'ready_for_approval' });
    renderBar();
    await user.click(screen.getByRole('button', { name: 'Start voice recording' }));
    await user.click(screen.getByRole('button', { name: 'Stop recording' }));
    await waitFor(() => expect(screen.getByLabelText('Question')).toHaveValue('Change the property on this bill to Oak Street'));
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const panel = await screen.findByRole('region', { name: 'Proposed change' });
    expect(within(panel).getByText('Oak Street Duplex')).toBeInTheDocument();
    expect(within(panel).getByText('empty')).toBeInTheDocument();
    expect(m.updateDocument).not.toHaveBeenCalled();
    await user.click(within(panel).getByRole('button', { name: 'Confirm change' }));
    expect(m.updateDocument).toHaveBeenCalledWith('d9', { revision: 4, property_id: 'p2', source: 'voice' });
    expect(await screen.findByText(/Nothing was posted to QuickBooks/)).toBeInTheDocument();
  });

  it('cancelling the proposal leaves the record unchanged', async () => {
    const user = userEvent.setup();
    m.search.mockResolvedValue(proposal);
    renderBar();
    await user.type(screen.getByLabelText('Question'), 'Change the property on this bill to Oak Street');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const panel = await screen.findByRole('region', { name: 'Proposed change' });
    await user.click(within(panel).getByRole('button', { name: 'Cancel change' }));
    expect(m.updateDocument).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Proposed change' })).not.toBeInTheDocument();
    expect(screen.getByText('Change cancelled. The bill was not modified.')).toBeInTheDocument();
  });

  it('sends every field a proposal carries (vendor name plus QuickBooks vendor)', async () => {
    const user = userEvent.setup();
    m.search.mockResolvedValue(answer({
      intent: 'correction', answer: 'Proposed change. Nothing changes until you confirm.',
      proposed_edit: { document_id: 'd9', revision: 3, field: 'vendor_name', field_label: 'Vendor',
        update_key: 'reviewed_vendor_name', current_value: 'City Water', proposed_value: 'City Water Dept',
        proposed_raw: 'City Water Dept', updates: { reviewed_vendor_name: 'City Water Dept', qb_vendor_id: '57' } },
    }));
    m.updateDocument.mockResolvedValue({ success: true, revision: 4, status: 'ready_for_approval' });
    renderBar();
    await user.type(screen.getByLabelText('Question'), 'change the vendor to City Water Dept');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const panel = await screen.findByRole('region', { name: 'Proposed change' });
    await user.click(within(panel).getByRole('button', { name: 'Confirm change' }));
    expect(m.updateDocument).toHaveBeenCalledWith('d9', {
      revision: 3, reviewed_vendor_name: 'City Water Dept', qb_vendor_id: '57', source: 'typed' });
  });
});
