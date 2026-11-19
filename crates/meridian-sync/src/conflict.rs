//! `SYNC-CONFLICT.md`: how an agent working in a context directory learns that its clone
//! stopped on a conflict. The daemon writes it into each context directory of a conflicted
//! clone and deletes it after the first pass that syncs. A pass never commits it (`pass`
//! excludes it by pathspec) and the watcher ignores it.

use std::fs;
use std::io::ErrorKind;
use std::path::Path;

use crate::pass::Conflict;
use crate::records::ContextRef;

pub const FILE: &str = "SYNC-CONFLICT.md";

/// A directory inside the clone that one or more contexts name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Dir {
    /// Relative to the clone; empty when the context is the whole clone.
    pub rel: String,
    pub contexts: Vec<String>,
}

/// The distinct directories the contexts name. The records' paths are already confined to
/// the clone.
pub fn dirs(contexts: &[ContextRef]) -> Vec<Dir> {
    let mut out: Vec<Dir> = Vec::new();
    for c in contexts {
        let rel = c.path.clone().unwrap_or_default();
        match out.iter_mut().find(|d| d.rel == rel) {
            Some(d) if !d.contexts.contains(&c.name) => d.contexts.push(c.name.clone()),
            Some(_) => {}
            None => out.push(Dir {
                rel,
                contexts: vec![c.name.clone()],
            }),
        }
    }
    out
}

/// Writes the file for a conflicted pass (`Some`) or removes it after a synced one
/// (`None`). A directory that doesn't exist is skipped. A failure is logged and never
/// fails the pass: the state file still says the clone is stopped.
pub fn sync(clone: &Path, dirs: &[Dir], conflict: Option<&Conflict>) {
    for dir in dirs {
        let target = clone.join(&dir.rel);
        if !target.is_dir() {
            continue;
        }
        let file = target.join(FILE);
        let result = match conflict {
            Some(c) => fs::write(&file, body(dir, c)),
            None => fs::remove_file(&file).or_else(|e| match e.kind() {
                ErrorKind::NotFound => Ok(()),
                _ => Err(e),
            }),
        };
        if let Err(e) = result {
            eprintln!("meridian-sync: {}: {e}", file.display());
        }
    }
}

fn body(dir: &Dir, conflict: &Conflict) -> String {
    let names = dir.contexts.join(", ");
    let paths: String = conflict.paths.iter().map(|p| format!("- {p}\n")).collect();
    format!(
        "# Autosync is stopped: merge conflict\n\n\
         Local and remote changes to this directory's git clone conflict, so nothing is \
         syncing until they are resolved. These files conflict:\n\n{paths}\n\
         To resolve: run `meridian sync now {first}`. It shows each conflicting file with \
         both sides. Edit the files in the clone, commit, and run it again. This file goes \
         away once syncing works; it is never committed.\n\n\
         Contexts here: {names}\n\n```\n{messages}\n```\n",
        first = dir.contexts.first().map_or("", String::as_str),
        messages = conflict.messages,
    )
}
