"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { Crumbs, EmptyState, ListSkeleton, RetryAlert } from "@/components/ui";
import { FolderIcon } from "@/components/icons";
import { useMe } from "@/components/session";
import { useLoad } from "@/components/use-load";
import { api } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { describeLoadFailure } from "@/lib/messages";
import type { Project } from "@/lib/types";

export default function ProjectsPage() {
  const { orgId } = useParams<{ orgId: string }>();
  const org = useMe().orgs.find((candidate) => candidate.id === orgId);
  const [extra, setExtra] = useState<{ orgId: string; items: Project[]; hasMore: boolean } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);
  const first = useLoad(() => api.listProjects(orgId), `projects:${orgId}`);

  const more = extra?.orgId === orgId ? extra : null;
  const items = first.status === "ready" ? [...first.data.items, ...(more?.items ?? [])] : [];
  const hasMore = first.status === "ready" && (more ? more.hasMore : first.data.has_more);

  async function loadMore() {
    setLoadingMore(true);
    setMoreFailed(false);
    try {
      const page = await api.listProjects(orgId, items.length);
      setExtra({ orgId, items: [...(more?.items ?? []), ...page.items], hasMore: page.has_more });
    } catch {
      setMoreFailed(true);
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <>
      <Crumbs trail={[{ label: "Organizations", href: "/orgs" }, { label: org?.name ?? "Organization" }]} />
      <div className="page-head">
        <h1>{org ? `${org.name} projects` : "Projects"}</h1>
        <p className="muted">Pick a project to upload photos and see what has been processed.</p>
      </div>

      {first.status === "loading" ? <ListSkeleton label="Loading projects" /> : null}
      {first.status === "error" ? (
        <RetryAlert message={describeLoadFailure(first.error, "organization")} onRetry={first.reload} />
      ) : null}
      {first.status === "ready" && items.length === 0 ? (
        <div className="card">
          <EmptyState icon={<FolderIcon />} title="No projects yet">
            Projects created in this organization will show up here.
          </EmptyState>
        </div>
      ) : null}
      {items.length > 0 ? (
        <ul className="list card">
          {items.map((project) => (
            <li key={project.id}>
              <Link className="list-row" href={`/orgs/${orgId}/projects/${project.id}`}>
                <span className="list-row-main">
                  <span className="list-row-title">{project.name}</span>
                  <span className="muted small">Created {formatDate(project.created_at)}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
      {moreFailed ? <RetryAlert message="Couldn't load more projects." onRetry={loadMore} /> : null}
      {hasMore ? (
        <div className="center">
          <button type="button" className="btn" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Show more projects"}
          </button>
        </div>
      ) : null}
    </>
  );
}
