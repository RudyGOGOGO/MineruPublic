import { useQuery } from "@tanstack/react-query";

import { loadSandboxes } from "./api";

export function useSandboxes({ enabled = true }: { enabled?: boolean } = {}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["sandboxes"],
    queryFn: () => loadSandboxes(),
    enabled,
    refetchOnWindowFocus: false,
  });
  return { sandboxes: data ?? [], isLoading, error };
}
