/** Calls of the admin pages that have no method in `api`: the deployment probe. */

import { postJson } from "../api";

export interface ProbeResult {
  ok: boolean;
  /** the status of the model's answer; 0 when nothing came back in time */
  status: number;
  answer: string;
  ttft_ms: number | null;
  tokens_per_s: number | null;
  error: string | null;
}

/** «Проверить»: one short question to the deployment through the gateway. */
export const probeDeployment = (id: number) => postJson<ProbeResult>(`/api/admin/deployments/${id}/probe`);
