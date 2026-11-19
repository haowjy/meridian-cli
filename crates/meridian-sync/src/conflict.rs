//! `SYNC-CONFLICT.md`: how an agent working in a context directory learns that its clone
//! stopped on a conflict. The daemon writes it into each context directory of a conflicted
//! clone and deletes it after the first pass that syncs, when the clone goes off, and when
//! a directory stops being a context directory. A pass never commits it (`pass` excludes
//! it, and its temporary file, by pathspec) and the watcher ignores both.

use std::fs;
use std::io::ErrorKind;
use std::path::Path;

use crate::pass::PassResult;
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

/// Writes the file for a conflicted pass (`Some`) or removes it (`None`). A directory that doesn't exist is skipped. A failure is logged and never
/// fails the pass: the state file still says the clone is stopped.
pub fn sync(clone: &Path, dirs: &[Dir], conflicted: Option<&PassResult>) {
    for dir in dirs {
        let target = clone.join(&dir.rel);
        if !target.is_dir() {
            continue;
        }
        let file = target.join(FILE);
        let result = match conflicted {
            Some(r) => write_atomic(&file, &body(dir, r)),
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

/// Temporary name: kept in `pass`'s exclude glob and the watcher's ignore (a prefix match).
fn write_atomic(file: &Path, text: &str) -> std::io::Result<()> {
    let tmp = file.with_file_name(format!("{FILE}.tmp"));
    fs::write(&tmp, text)?;
    fs::rename(&tmp, file).inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

fn body(dir: &Dir, r: &PassResult) -> String {
    let names = dir.contexts.join(", ");
    let (paths, messages) = r.conflict.as_ref().map_or_else(
        || (String::new(), ""),
        |c| {
            (
                c.paths.iter().map(|p| format!("- {p}\n")).collect(),
                c.messages.as_str(),
            )
        },
    );
    format!(
        "# Autosync is stopped: merge conflict\n\n\
         Local and remote changes to this directory's git clone conflict, so nothing is \
         syncing until they are resolved. These files conflict:\n\n{paths}\n\
         To resolve: run `meridian sync now {first}`. It shows each conflicting file with \
         both sides. {fix} Then run `meridian sync now {first}` again. This file goes away \
         once syncing works; it is never committed.\n\n\
         Contexts here: {names}\n\n```\n{messages}\n```\n",
        first = dir.contexts.first().map_or("", String::as_str),
        fix = r.fix.as_deref().unwrap_or(""),
    )
}
