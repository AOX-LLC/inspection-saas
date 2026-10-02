// The shapes the API returns. The API is the authority; these only describe it.

export type Role = "owner" | "admin" | "inspector" | "viewer";

export interface OrgSummary {
  id: string;
  name: string;
  role: Role;
}

export interface Me {
  id: string;
  email: string;
  display_name: string;
  orgs: OrgSummary[];
}

export interface Project {
  id: string;
  name: string;
  created_at: string;
}

export interface ProjectPage {
  items: Project[];
  has_more: boolean;
}

export type PhotoStatus = "queued" | "processing" | "tiled" | "failed";

export interface Photo {
  id: string;
  file_id: string;
  original_filename: string | null;
  status: PhotoStatus;
  width: number | null;
  height: number | null;
  error: string | null;
  thumbnail_url: string | null;
  created_at: string;
}

export interface PhotoPage {
  items: Photo[];
  next_cursor: string | null;
}

export interface Progress {
  total: number;
  counts: Record<PhotoStatus, number>;
  finished: boolean;
}

export interface PresignedPost {
  url: string;
  fields: Record<string, string>;
}

export interface UploadStart {
  file_id: string;
  upload: PresignedPost;
  expires_in: number;
}

export interface DemoAccount {
  email: string;
  display_name: string;
  description: string;
}

export interface DemoAccounts {
  accounts: DemoAccount[];
  password: string;
}
