import { useEffect, useRef, useState } from 'react';
import { DOCUMENTS_CHANGED, importGmailMessages, transcribe } from '../api';
import { isImportCommand } from './SearchBar';

interface VoiceButtonProps {
  // Called with the transcript of a spoken question; the assistant answers it.
  onQuestion: (transcript: string) => void;
}

type Phase = 'idle' | 'recording' | 'transcribing' | 'importing';
type Notice = { kind: 'error' | 'info' | 'success'; text: string } | null;

const MIME_PREFERENCE = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return undefined;
  return MIME_PREFERENCE.find((t) => MediaRecorder.isTypeSupported(t));
}

// Talk button for the page header: speak a command or a question without opening a dialog first.
// "Import from inbox" runs here and reports back in a notice; any other sentence is handed to
// the assistant, which answers it.
export default function VoiceButton({ onQuestion }: VoiceButtonProps) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [notice, setNotice] = useState<Notice>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const cancelledRef = useRef(false);

  const releaseMicrophone = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
  };

  useEffect(() => releaseMicrophone, []);

  const runImport = async () => {
    setPhase('importing');
    try {
      const r = await importGmailMessages();
      if (r.error) {
        setNotice({ kind: 'error', text: `Import from inbox failed: ${r.error}` });
      } else {
        setNotice({
          kind: r.failed > 0 ? 'error' : 'success',
          text: `Imported ${r.imported} new document(s) from ${r.messages_found} labeled email(s). `
            + `Already processed: ${r.already_processed}, skipped: ${r.skipped}, failed: ${r.failed}.`,
        });
        window.dispatchEvent(new Event(DOCUMENTS_CHANGED));
      }
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Import from inbox failed.' });
    } finally {
      setPhase('idle');
    }
  };

  const handleAudio = async (blob: Blob) => {
    setPhase('transcribing');
    try {
      const response = await transcribe(blob);
      const transcript = response.success ? (response.transcript || '').trim() : '';
      if (!transcript) {
        setNotice({ kind: 'error', text: response.error || 'I could not make out what was said. Try again.' });
        setPhase('idle');
        return;
      }
      if (isImportCommand(transcript)) {
        setNotice({ kind: 'info', text: `Heard: “${transcript}”. Importing…` });
        await runImport();
        return;
      }
      setPhase('idle');
      onQuestion(transcript);
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Transcription failed.' });
      setPhase('idle');
    }
  };

  const start = async () => {
    setNotice(null);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setNotice({ kind: 'error', text: 'Voice input needs a browser with microphone support over HTTPS. You can type instead.' });
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setNotice({ kind: 'error', text: 'Microphone access was denied. You can type your question instead.' });
      return;
    }
    const mimeType = pickMimeType();
    const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    streamRef.current = stream;
    recorderRef.current = recorder;
    chunksRef.current = [];
    cancelledRef.current = false;
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.onstop = () => {
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || mimeType || 'audio/webm' });
      chunksRef.current = [];
      releaseMicrophone();
      if (cancelledRef.current) {
        setPhase('idle');
        return; // a cancelled recording triggers no action
      }
      if (blob.size === 0) {
        setNotice({ kind: 'error', text: 'Nothing was recorded. Try again or type your question.' });
        setPhase('idle');
        return;
      }
      handleAudio(blob);
    };
    recorder.start();
    setPhase('recording');
  };

  const stop = () => recorderRef.current?.stop();

  const cancel = () => {
    cancelledRef.current = true;
    recorderRef.current?.stop();
    setNotice({ kind: 'info', text: 'Recording cancelled.' });
  };

  return (
    <>
      {phase === 'recording' ? (
        <>
          <button type="button" className="talk-btn recording" onClick={stop} aria-label="Stop and send">
            <span className="recording-dot" aria-hidden="true" />
            <span>Listening… tap to send</span>
          </button>
          <button type="button" className="btn-secondary" onClick={cancel} aria-label="Cancel voice command">Cancel</button>
        </>
      ) : (
        <button type="button" className="talk-btn" onClick={start} disabled={phase !== 'idle'}
          title="Talk: give a command or ask a question" aria-label="Talk">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
            <rect x="9" y="2" width="6" height="12" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0M12 18v4M8 22h8" />
          </svg>
          <span>{phase === 'transcribing' ? 'Transcribing…' : phase === 'importing' ? 'Importing…' : 'Talk'}</span>
        </button>
      )}

      {notice && (
        <div className={`voice-toast notice-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}
          aria-label="Voice command result">
          <span>{notice.text}</span>
          <button type="button" className="toast-close" onClick={() => setNotice(null)} aria-label="Dismiss">×</button>
        </div>
      )}
    </>
  );
}
