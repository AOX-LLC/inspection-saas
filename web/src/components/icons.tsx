import type { ReactNode } from "react";

// One icon set: 24px grid, 1.75 stroke, round caps. Decorative by default, because
// every icon sits next to words that say the same thing.
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}

export const UploadIcon = () => (
  <Icon>
    <path d="M12 16V4" />
    <path d="m7 9 5-5 5 5" />
    <path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
  </Icon>
);

export const CheckIcon = () => (
  <Icon>
    <circle cx="12" cy="12" r="9" />
    <path d="m8 12.5 2.8 2.8L16 9.5" />
  </Icon>
);

export const AlertIcon = () => (
  <Icon>
    <path d="M12 3.5 2.8 19.5h18.4Z" />
    <path d="M12 10v4.5" />
    <path d="M12 17.4v.1" />
  </Icon>
);

export const ClockIcon = () => (
  <Icon>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </Icon>
);

export const RetryIcon = () => (
  <Icon>
    <path d="M20 12a8 8 0 1 1-2.6-5.9" />
    <path d="M20 4v4.5h-4.5" />
  </Icon>
);

export const ImageIcon = () => (
  <Icon>
    <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="m4 17.5 5-4.5 4 3.5 3-2.5 4 3.5" />
  </Icon>
);

export const FolderIcon = () => (
  <Icon>
    <path d="M3.5 7.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2.5h8a1.5 1.5 0 0 1 1.5 1.5V17.5a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5Z" />
  </Icon>
);

export const SpinnerIcon = () => (
  <Icon>
    <path d="M12 3a9 9 0 1 0 9 9" />
  </Icon>
);

// The product mark: a frame with a crack through it.
export function BrandMark() {
  return (
    <svg className="icon brand-mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="3.5" y="3.5" width="17" height="17" rx="3" />
      <path d="m9 3.5 2.5 5-3 3 4 3-2 6" />
    </svg>
  );
}
