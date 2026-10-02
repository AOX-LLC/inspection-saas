"use client";

import Link from "next/link";
import { EmptyState, RoleChip } from "@/components/ui";
import { FolderIcon } from "@/components/icons";
import { useMe } from "@/components/session";
import { plural } from "@/lib/format";

export default function OrgsPage() {
  const me = useMe();
  return (
    <>
      <div className="page-head">
        <h1>Organizations</h1>
        <p className="muted">
          Signed in as {me.display_name}. You belong to {plural(me.orgs.length, "organization")}.
        </p>
      </div>
      {me.orgs.length === 0 ? (
        <div className="card">
          <EmptyState icon={<FolderIcon />} title="You aren't in any organization yet">
            Ask an owner of an organization to add you.
          </EmptyState>
        </div>
      ) : (
        <ul className="list card">
          {me.orgs.map((org) => (
            <li key={org.id}>
              <Link className="list-row" href={`/orgs/${org.id}/projects`}>
                <span className="list-row-main">
                  <span className="list-row-title">{org.name}</span>
                  <span className="muted small">Open projects</span>
                </span>
                <RoleChip role={org.role} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
