import { useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { DOCUMENTS_CHANGED, importGmailMessages, search, transcribe, updateDocument } from '../api';
import type { SearchResponse } from '../types';
import { femaleVoice, warmUpVoices } from '../voice';
import StatusBadge from './StatusBadge';

interface SearchBarProps {
  onClose: () => void;
  // A question already spoken on the main page: shown in the box and asked straight away.
  initialQuery?: string;
}

const MIME_PREFERENCE = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return undefined;
  return MIME_PREFERENCE.find((t) => MediaRecorder.isTypeSupported(t));
}

// "Import from inbox", "check my email for new bills", "pull in my Gmail" ...
const IMPORT_COMMAND = /^\s*(?:please\s+|can you\s+|could you\s+)?(?:import|check|pull|fetch|scan|sync|load)\b.*\b(?:inbox|e-?mails?|gmail|mail)\b/i;

export function isImportCommand(text: string): boolean {
  return IMPORT_COMMAND.test(text);
}

export default function SearchBar({ onClose, initialQuery = '' }: SearchBarProps) {
  const location = useLocation();
  const routeDocId = location.pathname.match(/^\/documents\/([^/]+)/)?.[1];

  const [query, setQuery] = useState(initialQuery);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [contextDocId, setContextDocId] = useState<string | undefined>(routeDocId);
  const [message, setMessage] = useState<{ kind: 'error' | 'info' | 'success'; text: string } | null>(null);

  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [voiceError, setVoiceError] = useState('');
  const [fromVoice, setFromVoice] = useState(!!initialQuery);
  const [speaking, setSpeaking] = useState(false);
  const [saving, setSaving] = useState(false);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const cancelledRef = useRef(false);
  const lastAudioRef = useRef<Blob | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const askedInitialRef = useRef(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // The panel sits at the top of the page: bring it into view if the page was scrolled down.
    panelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    inputRef.current?.focus({ preventScroll: true });
    warmUpVoices(); // so the voice list is ready by the time Play answer is clicked
    return () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      if (typeof window !== 'undefined' && window.speechSynthesis) window.speechSynthesis.cancel();
    };
  }, []);

  const releaseMicrophone = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
  };

  // Voice or typed command: bring in the labeled emails, exactly like the Import From Inbox button.
  const runImport = async () => {
    setLoading(true);
    setMessage(null);
    setResult(null);
    try {
      const r = await importGmailMessages();
      if (r.error) {
        setMessage({ kind: 'error', text: `Import from inbox failed: ${r.error}` });
      } else {
        const text = `Imported ${r.imported} new document(s) from ${r.messages_found} labeled email(s). `
          + `Already processed: ${r.already_processed}, skipped: ${r.skipped}, failed: ${r.failed}.`;
        setMessage({ kind: r.failed > 0 ? 'error' : 'success', text });
        window.dispatchEvent(new Event(DOCUMENTS_CHANGED));
      }
    } catch (err) {
      setMessage({ kind: 'error', text: err instanceof Error ? err.message : 'Import from inbox failed.' });
    } finally {
      setLoading(false);
    }
  };

  const ask = async (text: string, selectId?: string) => {
    if (!text.trim() && !selectId) return;
    if (!selectId && isImportCommand(text)) {
      await runImport();
      return;
    }
    setLoading(true);
    setMessage(null);
    try {
      const response = await search(text, contextDocId, selectId);
      setResult(response);
      if (response.selected_document_id) setContextDocId(response.selected_document_id);
    } catch (err) {
      setMessage({ kind: 'error', text: err instanceof Error ? err.message : 'The question could not be answered.' });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (initialQuery && !askedInitialRef.current) {
      askedInitialRef.current = true;
      ask(initialQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runTranscription = async (blob: Blob) => {
    setTranscribing(true);
    setVoiceError('');
    try {
      const response = await transcribe(blob);
      if (response.success && response.transcript) {
        setQuery(response.transcript);
        setFromVoice(true);
        lastAudioRef.current = null; // transient audio is discarded once transcribed
        inputRef.current?.focus();
        // A spoken import command runs straight away; questions still wait for Ask.
        if (isImportCommand(response.transcript)) await runImport();
      } else {
        setVoiceError(response.error || 'Transcription failed.');
      }
    } catch (err) {
      setVoiceError(err instanceof Error ? err.message : 'Transcription failed.');
    } finally {
      setTranscribing(false);
    }
  };

  const startRecording = async () => {
    setVoiceError('');
    setMessage(null);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setVoiceError('Voice input needs a browser with microphone support over HTTPS. You can type instead.');
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setVoiceError('Microphone access was denied. You can type your question instead.');
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
      if (cancelledRef.current) return; // a cancelled recording triggers no action
      if (blob.size === 0) {
        setVoiceError('Nothing was recorded. Try again or type your question.');
        return;
      }
      lastAudioRef.current = blob;
      runTranscription(blob);
    };
    recorder.start();
    setRecording(true);
  };

  const stopRecording = () => {
    if (recorderRef.current && recording) {
      recorderRef.current.stop();
      setRecording(false);
    }
  };

  const cancelRecording = () => {
    if (recorderRef.current && recording) {
      cancelledRef.current = true;
      recorderRef.current.stop();
      setRecording(false);
      setMessage({ kind: 'info', text: 'Recording cancelled.' });
    }
  };

  const speak = () => {
    if (!result?.answer || !window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(result.answer);
    const voice = femaleVoice();
    if (voice) {
      utterance.voice = voice;
      utterance.lang = voice.lang;
    }
    utterance.onend = () => setSpeaking(false);
    utterance.onerror = () => setSpeaking(false);
    window.speechSynthesis.speak(utterance);
    setSpeaking(true);
  };

  const stopSpeaking = () => {
    window.speechSynthesis?.cancel();
    setSpeaking(false);
  };

  const confirmEdit = async () => {
    const edit = result?.proposed_edit;
    if (!edit) return;
    setSaving(true);
    try {
      const updates = edit.updates && Object.keys(edit.updates).length
        ? edit.updates : { [edit.update_key]: edit.proposed_raw };
      await updateDocument(edit.document_id, {
        revision: edit.revision, ...(updates as Record<string, string>),
        source: fromVoice ? 'voice' : 'typed',
      });
      setResult({ ...result!, proposed_edit: null });
      setMessage({ kind: 'success', text: `${edit.field_label} changed to ${edit.proposed_value}. Nothing was posted to QuickBooks.` });
    } catch (err) {
      setMessage({ kind: 'error', text: err instanceof Error ? err.message : 'The change could not be saved.' });
    } finally {
      setSaving(false);
    }
  };

  const cancelEdit = () => {
    setResult(result ? { ...result, proposed_edit: null } : null);
    setMessage({ kind: 'info', text: 'Change cancelled. The bill was not modified.' });
  };

  return (
    <div className="assistant-inline" ref={panelRef}>
      <div className="card assistant" role="region" aria-label="Ask about a bill">
        <form onSubmit={(e) => { e.preventDefault(); ask(query); }} className="stack">
          <label htmlFor="assistant-question">Question</label>
          <input id="assistant-question" ref={inputRef} type="text" value={query}
            onChange={(e) => setQuery(e.target.value)} placeholder="What happened to the electricity bill?"
            disabled={loading || recording || transcribing} />
          {fromVoice && query && !recording && !transcribing && (
            <p className="small muted">Transcript — check and edit it, then press Ask.</p>
          )}
          <div className="row wrap">
            {!recording ? (
              <button type="button" className="btn-secondary" onClick={startRecording} disabled={loading || transcribing}
                aria-label="Start voice recording">🎤 Talk</button>
            ) : (
              <>
                <button type="button" className="btn-danger" onClick={stopRecording} aria-label="Stop recording">Stop</button>
                <button type="button" className="btn-secondary" onClick={cancelRecording} aria-label="Cancel recording">Cancel</button>
              </>
            )}
            <button type="submit" className="btn-primary" disabled={loading || recording || transcribing || !query.trim()}>
              {loading ? 'Asking…' : 'Ask'}
            </button>
            <button type="button" className="btn-secondary" onClick={onClose}>Close</button>
          </div>
        </form>

        {recording && <div className="recording-indicator" role="status"><span className="recording-dot" /> Recording…</div>}
        {transcribing && <div role="status">Transcribing…</div>}
        {voiceError && (
          <div className="notice-error" role="alert">
            {voiceError}
            {lastAudioRef.current && !transcribing && (
              <> <button type="button" className="btn-secondary" onClick={() => runTranscription(lastAudioRef.current!)}>Retry transcription</button></>
            )}
          </div>
        )}
        {message && <div className={`notice-${message.kind}`} role={message.kind === 'error' ? 'alert' : 'status'}>{message.text}</div>}

        {result && (
          <div>
            <div className="answer" role="region" aria-label="Answer">
              <p>{result.answer}</p>
              <div className="row">
                {!speaking ? (
                  <button type="button" className="btn-secondary" onClick={speak}>Play answer</button>
                ) : (
                  <button type="button" className="btn-secondary" onClick={stopSpeaking}>Stop playback</button>
                )}
                <span className="small muted">Synthetic voice</span>
              </div>
            </div>

            {result.proposed_edit && (
              <div className="confirm-panel" role="region" aria-label="Proposed change">
                <h4>Proposed change</h4>
                <dl className="facts">
                  <dt>Field</dt><dd>{result.proposed_edit.field_label}</dd>
                  <dt>Current</dt><dd>{result.proposed_edit.current_value || 'empty'}</dd>
                  <dt>Proposed</dt><dd>{result.proposed_edit.proposed_value}</dd>
                </dl>
                <div className="row">
                  <button type="button" className="btn-primary" onClick={confirmEdit} disabled={saving}>Confirm change</button>
                  <button type="button" className="btn-secondary" onClick={cancelEdit} disabled={saving}>Cancel change</button>
                </div>
              </div>
            )}

            {result.needs_clarification && result.documents.length > 0 && (
              <div role="group" aria-label="Choose a bill">
                <p>{result.clarification_question}</p>
                {result.documents.map((d) => (
                  <button key={d.id} type="button" className="btn-secondary choice"
                    onClick={() => ask(result.pending_query || query, d.id)}>
                    {d.vendor_name || 'Unknown'} {d.invoice_number ? `#${d.invoice_number}` : ''} · {d.currency} {String(d.total ?? '')}
                  </button>
                ))}
              </div>
            )}

            {!result.needs_clarification && result.documents.map((d) => (
              <div key={d.id} className="evidence" role="region" aria-label="Sources">
                <div className="row-between">
                  <strong>{d.vendor_name || 'Unknown'} {d.invoice_number ? `#${d.invoice_number}` : ''}</strong>
                  <StatusBadge status={d.status} />
                </div>
                <div className="row wrap small">
                  {d.links.record && <Link to={d.links.record} onClick={onClose}>Open record</Link>}
                  {d.links.original && <a href={d.links.original} target="_blank" rel="noopener noreferrer">Original</a>}
                  {d.links.quickbooks && <a href={d.links.quickbooks} target="_blank" rel="noopener noreferrer">QuickBooks bill {d.qb_bill_doc_number || d.qb_bill_id}</a>}
                </div>
                {result.history.length > 0 && (
                  <ol className="plain history small" aria-label="Supporting history">
                    {result.history.map((e) => (
                      <li key={e.id}>{new Date(e.occurred_at + 'Z').toLocaleString()} — {e.summary}</li>
                    ))}
                  </ol>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
