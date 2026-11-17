import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({
  search: vi.fn(), transcribe: vi.fn(), updateDocument: vi.fn(), importGmailMessages: vi.fn(),
  DOCUMENTS_CHANGED: 'documents-changed',
}));

import * as api from '../api';
import VoiceButton from './VoiceButton';

const m = vi.mocked(api);
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

const imported = { success: true, messages_found: 1, attachments_processed: 1, imported: 1, already_processed: 0,
  skipped: 0, failed: 0, results: [] };

beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeRecorder;
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) } });
});

async function say(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Talk' }));
  await user.click(screen.getByRole('button', { name: 'Stop and send' }));
}

describe('Talk button on the main page', () => {
  it('imports from the inbox on a spoken command, with no dialog', async () => {
    const user = userEvent.setup();
    const onQuestion = vi.fn();
    const changed = vi.fn();
    window.addEventListener('documents-changed', changed);
    m.transcribe.mockResolvedValue({ success: true, transcript: 'Import from inbox.' });
    m.importGmailMessages.mockResolvedValue(imported);
    render(<VoiceButton onQuestion={onQuestion} />);
    await say(user);
    expect(await screen.findByRole('status', { name: 'Voice command result' })).toHaveTextContent(
      'Imported 1 new document(s) from 1 labeled email(s). Already processed: 0, skipped: 0, failed: 0.');
    expect(m.importGmailMessages).toHaveBeenCalledTimes(1);
    expect(onQuestion).not.toHaveBeenCalled();
    expect(changed).toHaveBeenCalledTimes(1);
    expect(track.stop).toHaveBeenCalled(); // microphone released
    window.removeEventListener('documents-changed', changed);
  });

  it('hands a spoken question to the assistant', async () => {
    const user = userEvent.setup();
    const onQuestion = vi.fn();
    m.transcribe.mockResolvedValue({ success: true, transcript: 'What happened to the power bill?' });
    render(<VoiceButton onQuestion={onQuestion} />);
    await say(user);
    await waitFor(() => expect(onQuestion).toHaveBeenCalledWith('What happened to the power bill?'));
    expect(m.importGmailMessages).not.toHaveBeenCalled();
  });

  it('cancelling sends nothing', async () => {
    const user = userEvent.setup();
    const onQuestion = vi.fn();
    render(<VoiceButton onQuestion={onQuestion} />);
    await user.click(screen.getByRole('button', { name: 'Talk' }));
    await user.click(screen.getByRole('button', { name: 'Cancel voice command' }));
    expect(m.transcribe).not.toHaveBeenCalled();
    expect(onQuestion).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Talk' })).toBeEnabled();
  });

  it('explains a denied microphone and a failed import', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockRejectedValue(new Error('denied')) } });
    const { unmount } = render(<VoiceButton onQuestion={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Talk' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Microphone access was denied');
    unmount();

    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) } });
    m.transcribe.mockResolvedValue({ success: true, transcript: 'check my email' });
    m.importGmailMessages.mockResolvedValue({ ...imported, success: false, imported: 0, error: 'Gmail is not connected' });
    render(<VoiceButton onQuestion={vi.fn()} />);
    await say(user);
    expect(await screen.findByRole('alert')).toHaveTextContent('Import from inbox failed: Gmail is not connected');
  });
});
