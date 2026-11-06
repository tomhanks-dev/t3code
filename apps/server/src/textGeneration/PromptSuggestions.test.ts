import type {
  ProjectionRecords,
  ProjectionRecordField,
} from "../orchestration-v2/ProjectionStore.ts";
import { describe, expect, it, vi } from "@effect/vitest";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  DEFAULT_SERVER_SETTINGS,
  ProjectId,
  RunId,
  ThreadId,
  type OrchestrationV2AppThread,
  type OrchestrationV2ThreadShell,
  type OrchestrationV2Run,
  type OrchestrationV2ConversationMessage,
} from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Threads from "../orchestration-v2/ThreadManagementService.ts";
import * as Settings from "../serverSettings.ts";
import * as Generation from "./TextGeneration.ts";
import * as Suggestions from "./PromptSuggestions.ts";

const threadId = ThreadId.make("suggestions-thread");
const runId = RunId.make("suggestions-run");
const input = { threadId, runId };

function fixture(generate?: Generation.TextGeneration["Service"]["generatePromptSuggestion"]) {
  let shell = {
    latestRunId: runId,
    activeRunId: null,
    pendingRuntimeRequest: null,
  } as OrchestrationV2ThreadShell;
  let status: OrchestrationV2Run["status"] = "completed";
  const generation = vi.fn(
    generate ??
      (() => Effect.succeed({ suggestion: "Continue implementing the export, then verify it." })),
  );
  const records = vi.fn(() =>
    Effect.succeed<ProjectionRecords<ProjectionRecordField>>({
      attempts: [],
      nodes: [],
      subagents: [],
      providerSessions: [],
      providerThreads: [],
      providerTurns: [],
      runtimeRequests: [],
      plans: [],
      turnItems: [],
      checkpointScopes: [],
      checkpoints: [],
      contextHandoffs: [],
      contextTransfers: [],
      thread: { projectId: ProjectId.make("project") } as OrchestrationV2AppThread,
      runs: [{ id: runId, status }] as OrchestrationV2Run[],
      messages: [
        { role: "user", text: "Implement CSV export.", runId, streaming: false },
        {
          role: "assistant",
          text: "I can finish the export next. Shall I proceed?",
          runId,
          streaming: false,
        },
      ] as OrchestrationV2ConversationMessage[],
    }),
  );
  const layer = Suggestions.layer.pipe(
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(Threads.ThreadManagementService)({
          getThreadShell: () => Effect.sync(() => shell),
          getThreadRecords: records,
        }),
        Layer.mock(Settings.ServerSettingsService)({
          getSettings: Effect.succeed(DEFAULT_SERVER_SETTINGS),
        }),
        Layer.mock(Generation.TextGeneration)({ generatePromptSuggestion: generation }),
        NodeServices.layer,
      ),
    ),
  );
  return {
    layer,
    generation,
    records,
    setShell: (next: Partial<OrchestrationV2ThreadShell>) => {
      shell = { ...shell, ...next };
    },
    setStatus: (next: typeof status) => {
      status = next;
    },
  };
}

describe("prompt suggestions", () => {
  it.effect("uses conversation context and shares one generation per completed turn", () => {
    const f = fixture();
    return Effect.gen(function* () {
      const service = yield* Suggestions.PromptSuggestions;
      const results = yield* Effect.all([service.generate(input), service.generate(input)], {
        concurrency: "unbounded",
      });
      expect(results[0]).toEqual({
        suggestion: "Continue implementing the export, then verify it.",
      });
      expect(results[1]).toEqual(results[0]);
      expect(yield* service.generate(input)).toEqual(results[0]);
      expect(f.generation).toHaveBeenCalledTimes(1);
      const request = f.generation.mock.calls[0]![0];
      expect(request.context).toContain("Implement CSV export.");
      expect(request.context).toContain("Shall I proceed?");
      expect(request.modelSelection).toEqual(DEFAULT_SERVER_SETTINGS.textGenerationModelSelection);
    }).pipe(Effect.provide(f.layer));
  });

  it.effect("suppresses stale, running, rolled-back, and pending-request turns", () => {
    const f = fixture();
    return Effect.gen(function* () {
      const service = yield* Suggestions.PromptSuggestions;
      f.setShell({ activeRunId: RunId.make("new-run") });
      expect(yield* service.generate(input)).toEqual({ suggestion: "" });
      f.setShell({ activeRunId: null, latestRunId: RunId.make("new-run") });
      expect(yield* service.generate(input)).toEqual({ suggestion: "" });
      f.setShell({
        activeRunId: null,
        latestRunId: runId,
        pendingRuntimeRequest: { kind: "user_input" } as NonNullable<
          OrchestrationV2ThreadShell["pendingRuntimeRequest"]
        >,
      });
      expect(yield* service.generate(input)).toEqual({ suggestion: "" });
      f.setShell({ pendingRuntimeRequest: null });
      f.setStatus("rolled_back");
      expect(yield* service.generate(input)).toEqual({ suggestion: "" });
      expect(f.generation).not.toHaveBeenCalled();
    }).pipe(Effect.provide(f.layer));
  });

  it.effect("discards a generation that finishes after a new turn starts", () =>
    Effect.gen(function* () {
      const started = yield* Deferred.make<void>();
      const finish = yield* Deferred.make<void>();
      const f = fixture(() =>
        Deferred.succeed(started, undefined).pipe(
          Effect.andThen(Deferred.await(finish)),
          Effect.as({ suggestion: "Continue." }),
        ),
      );
      yield* Effect.gen(function* () {
        const service = yield* Suggestions.PromptSuggestions;
        const pending = yield* service.generate(input).pipe(Effect.forkChild);
        yield* Deferred.await(started);
        f.setShell({ activeRunId: RunId.make("new-run") });
        yield* Deferred.succeed(finish, undefined);
        expect(yield* Fiber.join(pending)).toEqual({ suggestion: "" });
      }).pipe(Effect.provide(f.layer));
    }),
  );
});
