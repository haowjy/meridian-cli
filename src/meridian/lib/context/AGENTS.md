# lib/context/

Converts symbolic `ContextConfig` into concrete filesystem paths (`ResolvedContextPaths`)
for work directories, KB roots, and named contexts. A pure derivation: it never creates
paths, clones repos, or touches the filesystem. Agents receive the paths as
`MERIDIAN_CONTEXT_{NAME}_DIR` (`context_env_key(name)`). Entry point `resolver.py`;
placeholder rules and env naming: `.context/CONTEXT.md`.

## Rules

- **`{project}` resolves through `state/project_identity.py`; read-only use never mints
  or materializes identity.** An unmaterialized ad-hoc path stays unresolved until a
  write; project-relative paths stay unresolved for ad-hoc places so bootstrap never
  writes into them. Extra contexts requiring identity are skipped, with no repo-local
  fallback and no state created.
- **Git-backed contexts are not cloned here.** `_resolve_path()` only derives the
  expected path via `resolve_clone_path(remote)`; git-autosync hooks do the cloning.
- Defaults are user-scoped: `{user_home}/context/{project}/work` and `.../kb`.
