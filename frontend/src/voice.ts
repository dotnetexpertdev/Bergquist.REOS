// Choosing the voice for "Play answer". Browsers offer different voices, so this picks the best
// available female English voice and falls back to the browser default when there is none.

// Best first: natural-sounding online voices, then the standard ones shipped with Windows, Chrome and macOS.
const FEMALE_VOICES = [
  /aria/i, /jenny/i, /ava\b/i, /emma/i, /michelle/i, /sonia/i, /libby/i,
  /zira/i, /google us english/i, /google uk english female/i,
  /samantha/i, /allison/i, /susan/i, /hazel/i, /karen/i, /moira/i, /tessa/i, /victoria/i,
  /female/i,
];

export function pickFemaleVoice(voices: SpeechSynthesisVoice[]): SpeechSynthesisVoice | null {
  const english = voices.filter((v) => /^en([-_]|$)/i.test(v.lang || ''));
  for (const pool of [english, voices]) {
    for (const pattern of FEMALE_VOICES) {
      const match = pool.find((v) => pattern.test(v.name));
      if (match) return match;
    }
  }
  return null;
}

// Chrome fills the voice list a moment after the page loads; asking early starts that loading.
export function warmUpVoices(): void {
  try {
    window.speechSynthesis?.getVoices?.();
  } catch {
    // speech is optional
  }
}

export function femaleVoice(): SpeechSynthesisVoice | null {
  try {
    return pickFemaleVoice(window.speechSynthesis?.getVoices?.() || []);
  } catch {
    return null;
  }
}
