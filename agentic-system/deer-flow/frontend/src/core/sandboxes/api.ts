import { getBackendBaseURL } from "../config";

import type { SandboxInfo } from "./types";

export async function loadSandboxes(): Promise<SandboxInfo[]> {
  const res = await fetch(`${getBackendBaseURL()}/api/sandboxes`);
  const { sandboxes } = (await res.json()) as { sandboxes: SandboxInfo[] };
  return sandboxes;
}
