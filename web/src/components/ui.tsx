import Link from "next/link";
import type { ReactNode } from "react";
import { AlertIcon, CheckIcon, SpinnerIcon } from "./icons";

export function Alert({
  tone,
  children,
  action,
}: {
  tone: "danger" | "warn" | "ok";
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "danger" ? "alert" : "status"}>
      {tone === "ok" ? <CheckIcon /> : <AlertIcon />}
      <div className="alert-body">{children}</div>
      {action}
    </div>
  );
}

export function RetryAlert({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Alert
      tone="danger"
      action={
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Try again
        </button>
      }
    >
      {message}
    </Alert>
  );
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <p className="empty-title">{title}</p>
      {children ? <p>{children}</p> : null}
    </div>
  );
}

export function ListSkeleton({ rows = 3, label }: { rows?: number; label: string }) {
  return (
    <ul className="list card" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, index) => (
        <li key={index}>
          <div className="skeleton skeleton-row" />
        </li>
      ))}
    </ul>
  );
}

export function Busy({ children }: { children: ReactNode }) {
  return (
    <p className="muted small" role="status">
      <SpinnerIcon /> {children}
    </p>
  );
}

export interface Crumb {
  label: string;
  href?: string;
}

export function Crumbs({ trail }: { trail: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb">
      <ol className="crumbs">
        {trail.map((crumb, index) => (
          <li key={index} aria-current={index === trail.length - 1 ? "page" : undefined}>
            {crumb.href ? <Link href={crumb.href}>{crumb.label}</Link> : crumb.label}
          </li>
        ))}
      </ol>
    </nav>
  );
}

const ROLE_LABELS: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  inspector: "Inspector",
  viewer: "Viewer",
};

export function RoleChip({ role }: { role: string }) {
  return <span className="chip">{ROLE_LABELS[role] ?? role}</span>;
}
