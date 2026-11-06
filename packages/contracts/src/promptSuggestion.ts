import * as Schema from "effect/Schema";
import { RunId, ThreadId } from "./baseSchemas.ts";

export const PromptSuggestionInput = Schema.Struct({ threadId: ThreadId, runId: RunId });
export type PromptSuggestionInput = typeof PromptSuggestionInput.Type;
export const PromptSuggestionResult = Schema.Struct({ suggestion: Schema.String });
export class PromptSuggestionError extends Schema.TaggedError<PromptSuggestionError>()(
  "PromptSuggestionError",
  { cause: Schema.Defect() },
) {
  override get message(): string {
    return "Could not suggest a next prompt.";
  }
}
