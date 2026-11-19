//! File watching: a hint that something may have changed. `git status` inside the pass
//! decides whether it did.
//!
//! Linux (inotify) takes one non-recursive watch per directory git doesn't ignore, found
//! with the `ignore` crate; a clone that needs more than [`MAX_DIRS`] watches, or hits the
//! kernel's limit, is polled instead. macOS (FSEvents) takes one recursive stream and
//! drops events under paths the root ignore files name.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use ignore::gitignore::{Gitignore, GitignoreBuilder};
use ignore::WalkBuilder;
use notify::event::{EventKind, ModifyKind};
use notify::{Event, RecommendedWatcher, RecursiveMode, Watcher};

/// Watched directories per clone before falling back to polling.
pub const MAX_DIRS: usize = 20_000;

const CONFLICT_FILE: &str = "SYNC-CONFLICT.md";

/// What the watcher tells the worker.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Hint {
    /// Something in the worktree changed.
    Change,
    /// `HEAD` in the git dir changed: the branch moved.
    Head,
    /// The event queue overflowed: events were lost.
    Rescan,
    /// A directory appeared; on Linux it needs watches of its own.
    NewDir(PathBuf),
    /// The watcher can't continue (the kernel's watch limit): poll instead.
    Failed,
}

/// The watcher can't take another watch: the caller polls instead.
#[derive(Debug)]
pub struct LimitReached;

pub type Sink = Arc<dyn Fn(Hint) + Send + Sync>;

pub struct Watch {
    watcher: RecommendedWatcher,
    clone: PathBuf,
    recursive: bool,
    /// Directories holding a watch.
    watched: HashSet<PathBuf>,
}

struct Classifier {
    git_dir: PathBuf,
    ignored: Option<Gitignore>,
}

impl Classifier {
    fn classify(&self, event: &Event, recursive: bool) -> Vec<Hint> {
        if event.need_rescan() {
            return vec![Hint::Rescan];
        }
        if matches!(event.kind, EventKind::Access(_)) {
            return Vec::new();
        }
        let mut hints = Vec::new();
        for path in &event.paths {
            if path.starts_with(&self.git_dir) {
                // Only HEAD matters inside the git dir; everything else is git's own
                // traffic, including what the pass itself writes.
                if path == &self.git_dir.join("HEAD") && !hints.contains(&Hint::Head) {
                    hints.push(Hint::Head);
                }
                continue;
            }
            if path.file_name().is_some_and(|n| n == CONFLICT_FILE) {
                continue;
            }
            if let Some(ignored) = &self.ignored {
                let is_dir = path.is_dir();
                if ignored
                    .matched_path_or_any_parents(path, is_dir)
                    .is_ignore()
                {
                    continue;
                }
            }
            let appeared = matches!(
                event.kind,
                EventKind::Create(_) | EventKind::Modify(ModifyKind::Name(_))
            );
            if !recursive && appeared && path.is_dir() {
                hints.push(Hint::NewDir(path.clone()));
            }
            if !hints.contains(&Hint::Change) {
                hints.push(Hint::Change);
            }
        }
        hints
    }
}

/// Starts watching `clone`. `None` means the clone can't be watched natively and the
/// caller polls.
pub fn start(clone: &Path, sink: Sink) -> Option<Watch> {
    let recursive = cfg!(target_os = "macos");
    let git_dir = clone.join(".git");
    let classifier = Classifier {
        git_dir: git_dir.clone(),
        ignored: recursive.then(|| root_ignores(clone)),
    };
    let handler_sink = Arc::clone(&sink);
    let handler = move |res: notify::Result<Event>| match res {
        Ok(event) => {
            for hint in classifier.classify(&event, recursive) {
                handler_sink(hint);
            }
        }
        Err(e) => {
            if is_limit(&e) {
                handler_sink(Hint::Failed);
            } else {
                // Unknown loss of events: look again rather than trust the silence.
                handler_sink(Hint::Rescan);
            }
        }
    };
    let watcher = notify::recommended_watcher(handler).ok()?;
    let mut watch = Watch {
        watcher,
        clone: clone.to_path_buf(),
        recursive,
        watched: HashSet::new(),
    };
    watch.watch_all().ok()?;
    Some(watch)
}

impl Watch {
    fn watch_all(&mut self) -> Result<(), LimitReached> {
        let clone = self.clone.clone();
        if self.recursive {
            self.add(&clone, RecursiveMode::Recursive)?;
        } else {
            self.add_tree(&clone)?;
        }
        let git_dir = clone.join(".git");
        if git_dir.is_dir() && !self.recursive {
            self.add(&git_dir, RecursiveMode::NonRecursive)?;
        }
        Ok(())
    }

    /// Watches `dir` and every directory below it that git doesn't ignore. The ignore
    /// files are read from the clone's root down, so a new directory is judged as the
    /// initial walk would judge it.
    pub fn add_tree(&mut self, dir: &Path) -> Result<(), LimitReached> {
        if self.recursive {
            return Ok(());
        }
        for found in unignored_dirs(&self.clone, dir) {
            self.add(&found, RecursiveMode::NonRecursive)?;
        }
        Ok(())
    }

    /// After a queue overflow: watch what appeared while events were lost, and forget
    /// what is gone, so a clone that shrank can leave the polling fallback.
    pub fn rewalk(&mut self) -> Result<(), LimitReached> {
        self.watched.retain(|p| p.is_dir());
        self.watch_all()
    }

    fn add(&mut self, path: &Path, mode: RecursiveMode) -> Result<(), LimitReached> {
        if self.watched.contains(path) {
            return Ok(());
        }
        if self.watched.len() >= MAX_DIRS {
            return Err(LimitReached);
        }
        match self.watcher.watch(path, mode) {
            Ok(()) => {
                self.watched.insert(path.to_path_buf());
                Ok(())
            }
            Err(e) if is_limit(&e) => Err(LimitReached),
            // This directory's own problem (gone, or not readable): the rest is watched.
            Err(_) => Ok(()),
        }
    }
}

/// Directories at or below `subtree` that git doesn't ignore, walking from the clone's
/// root so every ignore file above them applies. Other branches of the tree are listed,
/// not entered.
fn unignored_dirs(clone: &Path, subtree: &Path) -> Vec<PathBuf> {
    let subtree = subtree.to_path_buf();
    let on_path = subtree.clone();
    WalkBuilder::new(clone)
        .hidden(false)
        .parents(false)
        .require_git(false)
        .git_ignore(true)
        .git_exclude(true)
        .git_global(true)
        .filter_entry(move |e| {
            e.file_name() != ".git"
                && (e.path().starts_with(&on_path) || on_path.starts_with(e.path()))
        })
        .build()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_some_and(|t| t.is_dir()))
        .map(ignore::DirEntry::into_path)
        .filter(|p| p.starts_with(&subtree))
        .collect()
}

fn is_limit(e: &notify::Error) -> bool {
    matches!(e.kind, notify::ErrorKind::MaxFilesWatch)
        || matches!(&e.kind, notify::ErrorKind::Io(io) if io.raw_os_error() == Some(libc::ENOSPC))
}

fn root_ignores(clone: &Path) -> Gitignore {
    let mut builder = GitignoreBuilder::new(clone);
    builder.add(clone.join(".gitignore"));
    builder.add(clone.join(".git/info/exclude"));
    builder.build().unwrap_or_else(|_| Gitignore::empty())
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    use super::*;

    fn collect(clone: &Path) -> (Watch, Arc<Mutex<Vec<Hint>>>) {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let sink_seen = Arc::clone(&seen);
        let sink: Sink = Arc::new(move |h| sink_seen.lock().unwrap().push(h));
        (start(clone, sink).expect("watchable"), seen)
    }

    fn wait_for(seen: &Mutex<Vec<Hint>>, want: impl Fn(&Hint) -> bool) -> bool {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if seen.lock().unwrap().iter().any(&want) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        false
    }

    fn event(kind: EventKind, path: PathBuf) -> Event {
        Event::new(kind).add_path(path)
    }

    #[test]
    fn the_conflict_file_and_git_internals_are_not_changes() {
        let c = Classifier {
            git_dir: PathBuf::from("/c/.git"),
            ignored: None,
        };
        let create = EventKind::Create(notify::event::CreateKind::File);
        assert!(c
            .classify(&event(create, "/c/notes/SYNC-CONFLICT.md".into()), false)
            .is_empty());
        assert!(c
            .classify(&event(create, "/c/.git/index.lock".into()), false)
            .is_empty());
        assert_eq!(
            c.classify(&event(create, "/c/.git/HEAD".into()), false),
            vec![Hint::Head]
        );
        assert_eq!(
            c.classify(&event(create, "/c/notes/a.md".into()), false),
            vec![Hint::Change]
        );
    }

    #[test]
    fn ignored_directories_are_never_walked_old_or_new() {
        let tmp = tempfile::tempdir().unwrap();
        let clone = tmp.path();
        let mk = |p: &str| std::fs::create_dir_all(clone.join(p)).unwrap();
        std::fs::write(clone.join(".gitignore"), "build/\ncache/\n").unwrap();
        mk("build/deep");
        mk("notes/new/cache/x/y");
        mk("notes/new/inner");
        mk(".git");
        let names = |dirs: Vec<PathBuf>| -> Vec<String> {
            let mut v: Vec<_> = dirs
                .iter()
                .map(|d| d.strip_prefix(clone).unwrap().display().to_string())
                .collect();
            v.sort();
            v
        };
        assert_eq!(
            names(unignored_dirs(clone, clone)),
            ["", "notes", "notes/new", "notes/new/inner"]
        );
        // A directory that appears later gets the same answer as in the first walk.
        assert_eq!(
            names(unignored_dirs(clone, &clone.join("notes/new"))),
            ["notes/new", "notes/new/inner"]
        );
        assert!(unignored_dirs(clone, &clone.join("build/deep")).is_empty());
        assert!(unignored_dirs(clone, &clone.join("notes/new/cache/x")).is_empty());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_live_watch_reports_changes_and_head() {
        let tmp = tempfile::tempdir().unwrap();
        let clone = tmp.path();
        std::fs::create_dir_all(clone.join(".git")).unwrap();
        std::fs::create_dir_all(clone.join("notes")).unwrap();
        let (watch, seen) = collect(clone);
        // root, notes, .git
        assert_eq!(watch.watched.len(), 3);
        std::fs::write(clone.join("notes/a.md"), "a").unwrap();
        assert!(wait_for(&seen, |h| *h == Hint::Change));
        std::fs::write(clone.join(".git/HEAD"), "ref: refs/heads/x\n").unwrap();
        assert!(wait_for(&seen, |h| *h == Hint::Head));
        std::fs::create_dir_all(clone.join("notes/new")).unwrap();
        assert!(wait_for(
            &seen,
            |h| matches!(h, Hint::NewDir(p) if p.ends_with("notes/new"))
        ));
    }
}
