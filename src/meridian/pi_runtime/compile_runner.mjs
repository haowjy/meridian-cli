#!/usr/bin/env bun

import { main as piMain } from "@earendil-works/pi-coding-agent";

if (typeof piMain !== "function") {
  console.error("meridian-pi: Pi SDK main(args, options?) export is unavailable.");
  process.exit(1);
}

const args = process.argv.slice(2);
await piMain(args, {});
