/** Versions of the node image and the «Обновить» requests (portal task 2-30: /api/admin/versions, /api/admin/nodes/.../update). */

import { deleteJson, getJson, postJson } from "../api";

export type UpdateWhen = "idle" | "now";

export interface NodeUpdateRequest {
  when: UpdateWhen;
  at: string | null;
  by: string | null;
  /** asked, but the node serves a deployment and «после выключения развёртывания» is chosen */
  waiting: boolean;
}

export interface NodeVersion {
  name: string;
  source: string;
  accel: string;
  ready: boolean;
  version: string | null;
  revision: string | null;
  digest: string | null;
  reported_at: string | null;
  /** another image than main's registry offers for the node; null: cannot be told (no report, no image in the registry) */
  outdated: boolean | null;
  update: NodeUpdateRequest | null;
  update_error: string | null;
  update_failed_at: string | null;
  /** when the node last asked main «should I update»; null: never, i.e. no updater runs on it */
  updater_seen_at: string | null;
}

export interface RegistryTag {
  tag: string;
  current: boolean;
  accel: string | null;
  digest: string | null;
  version: string | null;
  revision: string | null;
  created: string | null;
}

export interface OfferedImage {
  tag: string;
  digest: string;
  version: string | null;
  revision: string | null;
}

export interface VersionGroup {
  version: string | null;
  revision: string | null;
  digest: string | null;
  outdated: boolean | null;
  count: number;
  nodes: string[];
}

export interface VersionsResponse {
  at: string;
  main: { version: string | null; revision: string | null; digest: string | null };
  registry: { repo: string; available: boolean; error: string | null; tags: RegistryTag[] };
  offered: { cpu: OfferedImage | null; nvidia: OfferedImage | null };
  auto_update: boolean;
  nodes: NodeVersion[];
  groups: VersionGroup[];
}

export interface UpdateResult {
  name: string;
  ok: boolean;
  error?: string;
  when?: UpdateWhen;
  waiting?: boolean;
}

export const fetchVersions = () => getJson<VersionsResponse>("/api/admin/versions");

export const requestNodeUpdate = (name: string, when: UpdateWhen, force = false) =>
  postJson<UpdateResult>(`/api/admin/nodes/${encodeURIComponent(name)}/update`, { when, force });

export const requestNodesUpdate = (names: string[], when: UpdateWhen) =>
  postJson<{ results: UpdateResult[] }>("/api/admin/nodes/update", { names, when });

export const cancelNodeUpdate = (name: string) => deleteJson(`/api/admin/nodes/${encodeURIComponent(name)}/update`);

/** «sha256:0123…» -> «0123456789ab» */
export const shortDigest = (digest: string | null): string => (digest ? digest.replace(/^sha256:/, "").slice(0, 12) : "");

/** «0.1.0 · abc1234»: the version and what tells the build apart (the commit, else the start of the image digest). */
export function versionText(v: { version: string | null; revision: string | null; digest: string | null }): string {
  const build = v.revision && v.revision !== "unknown" ? v.revision.slice(0, 7) : shortDigest(v.digest);
  return [v.version, build].filter(Boolean).join(" · ") || "нет данных";
}

export const whenText = (when: UpdateWhen): string => (when === "now" ? "сейчас" : "после выключения развёртывания");
