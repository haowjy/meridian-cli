#!/usr/bin/env node

let piMain;

try {
  ({ main: piMain } = await import("@earendil-works/pi-coding-agent"));
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(
    "meridian-pi: failed to import @earendil-works/pi-coding-agent. " +
      "Install wrapper deps with `npm install --prefix src/meridian/pi_runtime`."
  );
  console.error(`meridian-pi: cause: ${message}`);
  process.exit(1);
}

if (typeof piMain !== "function") {
  console.error("meridian-pi: Pi SDK main(args, options?) export is unavailable.");
  process.exit(1);
}

const args = process.argv.slice(2);
await piMain(args, {});
