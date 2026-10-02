import type {
  DemoAccounts,
  Me,
  Photo,
  PhotoPage,
  Progress,
  Project,
  ProjectPage,
  UploadStart,
} from "./types.ts";

// Every call goes to this origin's /api, which the web server forwards to the API.
// The session cookie is HttpOnly and same-origin, so nothing here handles a token.
const BASE = "/api";

// A failed call. `status` is 0 when the server could not be reached at all.
export class ApiError extends Error {
  readonly status: number;
  readonly retryAfter: number | null;

  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

// The API's `detail` is a sentence for 4xx errors and a list of field problems for 422.
function detailOf(body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return "";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: init.body ? { "Content-Type": "application/json", ...init.headers } : init.headers,
    });
  } catch {
    throw new ApiError(0, "unreachable");
  }
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const wait = Number(response.headers.get("Retry-After"));
    throw new ApiError(response.status, detailOf(body), Number.isFinite(wait) && wait > 0 ? wait : null);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const orgPath = (orgId: string) => `/orgs/${encodeURIComponent(orgId)}`;
const projectPath = (orgId: string, projectId: string) =>
  `${orgPath(orgId)}/projects/${encodeURIComponent(projectId)}`;

export const api = {
  login: (email: string, password: string) =>
    request<Me>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<Me>("/auth/me"),
  // Only in demo mode; anywhere else the API has no such route and this is a 404.
  demoAccounts: () => request<DemoAccounts>("/auth/demo-accounts"),

  listProjects: (orgId: string, offset = 0) =>
    request<ProjectPage>(`${orgPath(orgId)}/projects?limit=50&offset=${offset}`),
  getProject: (orgId: string, projectId: string) =>
    request<Project>(projectPath(orgId, projectId)),

  listPhotos: (orgId: string, projectId: string, limit: number, cursor: string | null = null) =>
    request<PhotoPage>(
      `${projectPath(orgId, projectId)}/photos?limit=${limit}` +
        (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""),
    ),
  progress: (orgId: string, projectId: string) =>
    request<Progress>(`${projectPath(orgId, projectId)}/photos/progress`),

  startUpload: (orgId: string, projectId: string, file: { name: string; type: string; size: number }) =>
    request<UploadStart>(`${projectPath(orgId, projectId)}/files`, {
      method: "POST",
      body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
    }),
  completeUpload: (orgId: string, projectId: string, fileId: string) =>
    request<unknown>(`${projectPath(orgId, projectId)}/files/${encodeURIComponent(fileId)}/complete`, {
      method: "POST",
    }),
};

export type { Photo };
