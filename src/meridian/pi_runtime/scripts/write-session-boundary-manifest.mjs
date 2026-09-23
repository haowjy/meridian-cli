import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.join(root, "dist/extensions/session-boundary/index.js");
const manifestPath = path.join(root, "dist/extensions/session-boundary/artifact.json");
const inputPaths = [
  "extensions/session-boundary/src/index.ts",
  "extensions/shared/session_boundary.ts",
  "package.json",
  "pnpm-lock.yaml",
  "scripts/write-session-boundary-manifest.mjs",
];
const flags = ["tsup", "--format", "esm", "--target", "node20", "--splitting", "false", "--external", "@earendil-works/pi-coding-agent", "--external", "@earendil-works/pi-tui"];
const hash = (value) => createHash("sha256").update(value).digest("hex");
const canonical = (value) => JSON.stringify(value);
const inputs = Object.fromEntries(inputPaths.map((file) => [file, hash(readFileSync(path.join(root, file)))]));
const outputSha256 = hash(readFileSync(output));
const identity = { schema: 1, flags, inputs, output_sha256: outputSha256 };
const manifest = { ...identity, artifact_id: hash(canonical(identity)) };
writeFileSync(manifestPath, `${JSON.stringify(manifest)}\n`, { mode: 0o600 });
