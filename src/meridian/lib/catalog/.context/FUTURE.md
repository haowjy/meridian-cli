- [ ] Review the apparently unused direct model lookup APIs in `models.py`,
  `catalog_session.py`, and `model_aliases.py`: `resolve_model`,
  `CatalogSession.resolve_model`, `CatalogSession.load_catalog`, and
  `load_mars_descriptions`. A source search found no production callers beyond
  their definitions and internal delegation; verify all CLI, MCP, and plugin
  surfaces before deleting them together or establishing a real owner. This
  predates the Possible/Curated/Selection rework and is not a P4 correctness
  blocker.
