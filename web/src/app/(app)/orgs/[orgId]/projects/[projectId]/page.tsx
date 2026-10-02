"use client";

import { useParams } from "next/navigation";
import { Crumbs, RetryAlert } from "@/components/ui";
import { ProjectView } from "@/components/project-view";
import { useMe } from "@/components/session";
import { useLoad } from "@/components/use-load";
import { api } from "@/lib/api";
import { describeLoadFailure } from "@/lib/messages";

export default function ProjectPage() {
  const { orgId, projectId } = useParams<{ orgId: string; projectId: string }>();
  const org = useMe().orgs.find((candidate) => candidate.id === orgId);
  const project = useLoad(() => api.getProject(orgId, projectId), `project:${orgId}/${projectId}`);

  const trail = [
    { label: "Organizations", href: "/orgs" },
    { label: org?.name ?? "Organization", href: `/orgs/${orgId}/projects` },
    { label: project.status === "ready" ? project.data.name : "Project" },
  ];

  return (
    <>
      <Crumbs trail={trail} />
      {project.status === "error" ? (
        <RetryAlert message={describeLoadFailure(project.error, "project")} onRetry={project.reload} />
      ) : (
        <>
          <div className="page-head">
            {project.status === "ready" ? <h1>{project.data.name}</h1> : <div className="skeleton skeleton-line" />}
            <p className="muted">Upload site photos. Finished ones appear in the grid below.</p>
          </div>
          <ProjectView orgId={orgId} projectId={projectId} />
        </>
      )}
    </>
  );
}
