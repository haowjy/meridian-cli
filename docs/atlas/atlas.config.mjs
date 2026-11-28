/*
 * The system atlas: Meridian, Mars, meridian-comms and how they meet.
 * Check it:  node tools/atlas/check.mjs docs/atlas/atlas.config.mjs
 * View it:   node tools/atlas/serve.mjs docs/atlas/atlas.config.mjs   (http://localhost:4848/atlas/)
 * tools/atlas/README.md explains every key. The engine's import checks read JS and TS only,
 * so for this Python tree the code checks are Paths, coverage and the state machine index.
 */
export default {
  title: "Meridian system atlas",
  root: "../..",
  repo: "haowjy/meridian-cli",
  branch: "main",
  pagesDir: "docs/atlas",
  // Each package directory, and the Rust crate, lies under some box's Paths.
  coverage: [
    { glob: "src/meridian/*" },
    { glob: "src/meridian/lib/*", needs: "__init__.py" },
    { glob: "crates/*" },
  ],
  stateMachines: {
    index: ".context/state-machines/index.md",
    pages: ":(glob)**/*state-machines*.md",
  },
  documents: [
    { title: "Design constraints", path: ".context/design-constraints.md" },
    { title: "Commands", path: "docs/commands.md" },
    { title: "Configuration", path: "docs/configuration.md" },
  ],
  nav: { machines: "State machines" },
};
