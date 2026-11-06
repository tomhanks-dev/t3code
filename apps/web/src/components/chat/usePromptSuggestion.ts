import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentId, RunId, ThreadId } from "@t3tools/contracts";
import { useEffect, useEffectEvent, useState } from "react";
import { generatePromptSuggestion } from "../../state/promptSuggestions";
import { useAtomCommand } from "../../state/use-atom-command";

export function usePromptSuggestion(
  environmentId: EnvironmentId,
  threadId: ThreadId | null,
  runId: RunId | null,
) {
  const allowed = useAtomValue(generatePromptSuggestion.permissionAtom(environmentId));
  const generate = useAtomCommand(generatePromptSuggestion, { reportFailure: false });
  const request = useEffectEvent(generate);
  const key = `${environmentId}:${threadId}:${runId}`;
  const [result, setResult] = useState<{ key: string; suggestion: string } | null>(null);
  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- requests a suggestion from the destination environment when its completed turn changes
  useEffect(() => {
    if (!allowed || !threadId || !runId) return;
    let cancelled = false;
    void request({ environmentId, input: { threadId, runId } }).then((response) => {
      if (!cancelled && response._tag === "Success")
        setResult({ key, suggestion: response.value.suggestion });
    });
    return () => {
      cancelled = true;
    };
  }, [allowed, environmentId, threadId, runId, key]);
  return allowed && result?.key === key ? result.suggestion : "";
}
