// Branding shown in the header and on the sign-in page.
// WORKSPACE_NAME is the company this installation is set up for.
export const WORKSPACE_NAME = 'Bergquist';
export const PRODUCT_TAGLINE = 'Real Estate Operating System';

// Sunrise over a roofline: the "morning" in Morning Assistant.
export function BrandMark({ size = 38 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 40 40" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="bm-bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#2a6450" />
          <stop offset="1" stopColor="#123026" />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="11" fill="url(#bm-bg)" />
      <circle cx="20" cy="22" r="7.5" fill="#e3c27e" />
      <path d="M6 30 L20 18.5 L34 30 V34 H6 Z" fill="#123026" />
      <path d="M6 30 L20 18.5 L34 30" fill="none" stroke="#f3efe4" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
      <rect x="18" y="27" width="4" height="7" rx="0.8" fill="#e3c27e" />
    </svg>
  );
}
