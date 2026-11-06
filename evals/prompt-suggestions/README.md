# Next prompt suggestions

Runs the shipped prompt and Codex structured generation path on synthetic conversations from the feature request and controls. From `evals/`:

```sh
pnpm prompt-suggestions --model <model> --out runs/suggestions
```

Use `--binary <codex executable>` when it is outside PATH. Each case runs three times. Review `review.json` against each case's rubric: continuation must finish the existing task; completed work and missing user information must produce no suggestion. Reject invented scope, destructive actions, or answers to unknown user choices. Outputs are limited to 240 characters by the shipped operation.

This new capability has no prior model prompt to compare. Preserve the controls when changing its guidance, and compare the same model and cases against the earlier revision's outputs.
