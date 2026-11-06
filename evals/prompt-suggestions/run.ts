import * as NodeUtil from "node:util";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { CodexSettings, ProviderInstanceId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as ServerConfig from "../../apps/server/src/config.ts";
import { makeCodexTextGeneration } from "../../apps/server/src/textGeneration/CodexTextGeneration.ts";
import { cases } from "./cases.ts";

const { values } = NodeUtil.parseArgs({
  options: {
    out: { type: "string" },
    model: { type: "string" },
    samples: { type: "string", default: "3" },
    binary: { type: "string", default: "codex" },
  },
});
const { out, model } = values;
const samples = Number(values.samples);
if (!out || !model || !Number.isSafeInteger(samples) || samples < 1)
  throw new Error("Use --out <directory> --model <model> [--binary <codex>] [--samples <count>].");
const decodeSettings = Schema.decodeEffect(CodexSettings);
const encodeRows = Schema.encodeEffect(Schema.fromJsonString(Schema.Unknown));
await Effect.runPromise(
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const cwd = yield* fs.makeTempDirectoryScoped({ prefix: "t3-suggestion-eval-" });
    const service = yield* makeCodexTextGeneration(
      yield* decodeSettings({ binaryPath: values.binary }),
    );
    const results = [];
    for (const fixture of cases) {
      for (let sample = 1; sample <= samples; sample++) {
        const result = yield* service.generatePromptSuggestion({
          cwd,
          context: fixture.context,
          modelSelection: { instanceId: ProviderInstanceId.make("codex"), model },
        });
        results.push({ ...fixture, sample, suggestion: result.suggestion });
      }
    }
    yield* fs.makeDirectory(out, { recursive: true });
    yield* fs.writeFileString(path.join(out, "review.json"), yield* encodeRows(results));
  }).pipe(
    Effect.provide(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-suggestion-eval-state-" }).pipe(
        Layer.provideMerge(NodeServices.layer),
      ),
    ),
    Effect.scoped,
  ),
);
