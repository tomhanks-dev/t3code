import { PromptSuggestionError, PromptSuggestionInput } from "@t3tools/contracts";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import * as Schema from "effect/Schema";
import * as Exit from "effect/Exit";
import * as Cache from "effect/Cache";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Threads from "../orchestration-v2/ThreadManagementService.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as TextGeneration from "./TextGeneration.ts";

export class PromptSuggestions extends Context.Service<
  PromptSuggestions,
  {
    readonly generate: (
      input: PromptSuggestionInput,
    ) => Effect.Effect<{ readonly suggestion: string }, PromptSuggestionError>;
  }
>()("t3/textGeneration/PromptSuggestions") {}

const decodeInput = Schema.decodeEffect(Schema.fromJsonString(PromptSuggestionInput));
const encodeInput = Schema.encodeEffect(Schema.fromJsonString(PromptSuggestionInput));

const make = Effect.gen(function* () {
  const threads = yield* Threads.ThreadManagementService;
  const settings = yield* ServerSettings.ServerSettingsService;
  const generation = yield* TextGeneration.TextGeneration;
  const fs = yield* FileSystem.FileSystem;
  const cached = yield* Cache.makeWith<
    string,
    { readonly suggestion: string },
    PromptSuggestionError
  >(
    Effect.fn("PromptSuggestions.generateCached")(
      function* (key: string) {
        const input = yield* decodeInput(key);
        const records = yield* threads.getThreadRecords(input.threadId, ["messages", "runs"], {
          messageRoles: ["user", "assistant"],
        });
        const run = records.runs.find((run) => run.id === input.runId);
        if (run?.status !== "completed") {
          return { suggestion: "" };
        }
        const retainedRunIds = new Set(
          records.runs.filter((run) => run.status !== "rolled_back").map((run) => run.id),
        );
        const messages = records.messages.filter(
          (message) =>
            !message.streaming && message.runId !== null && retainedRunIds.has(message.runId),
        );
        if (messages.at(-1)?.role !== "assistant") return { suggestion: "" };
        const firstRequest = messages.find((message) => message.role === "user");
        const recentMessages = messages.slice(-12);
        const originalRequest =
          firstRequest && !recentMessages.includes(firstRequest)
            ? `Original user request: ${firstRequest.text.slice(0, 3000)}

`
            : "";
        const recentContext = recentMessages
          .map((message) => `${message.role}: ${message.text.slice(-6000)}`)
          .join("\n\n")
          .slice(-20000);
        const context = originalRequest + recentContext;
        const projectSettings = resolveProjectSettings(
          yield* settings.getSettings,
          records.thread.projectId,
        ).settings;
        const cwd = yield* fs.makeTempDirectoryScoped({ prefix: "t3-prompt-suggestion-" });
        const result = yield* generation.generatePromptSuggestion({
          cwd,
          context,
          modelSelection: projectSettings.textGenerationModelSelection,
        });
        const suggestion = result.suggestion.trim();
        return { suggestion: suggestion.length <= 240 ? suggestion : "" };
      },
      Effect.scoped,
      Effect.mapError((cause) => new PromptSuggestionError({ cause })),
    ),
    {
      capacity: 128,
      timeToLive: (exit) => (Exit.isSuccess(exit) ? "1 day" : "30 seconds"),
    },
  );
  return PromptSuggestions.of({
    generate: Effect.fn("PromptSuggestions.generate")(function* (input) {
      const shell = yield* threads
        .getThreadShell(input.threadId)
        .pipe(Effect.mapError((cause) => new PromptSuggestionError({ cause })));
      if (
        shell?.latestRunId !== input.runId ||
        shell.activeRunId !== null ||
        shell.pendingRuntimeRequest !== null
      )
        return { suggestion: "" };
      const result = yield* Cache.get(
        cached,
        yield* encodeInput(input).pipe(
          Effect.mapError((cause) => new PromptSuggestionError({ cause })),
        ),
      );
      const current = yield* threads
        .getThreadShell(input.threadId)
        .pipe(Effect.mapError((cause) => new PromptSuggestionError({ cause })));
      return current?.latestRunId === input.runId &&
        current.activeRunId === null &&
        current.pendingRuntimeRequest === null
        ? result
        : { suggestion: "" };
    }),
  });
});
export const layer = Layer.effect(PromptSuggestions, make);
