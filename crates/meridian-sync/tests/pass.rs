//! The sync pass against a bare remote in a temp dir, through real git.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use meridian_sync::git::{Git, StepEvent, Timeouts};
use meridian_sync::pass::{run_pass, CloneSpec, Outcome, PassEnv, PassResult, Reason};

/// Every git call here, ours and the helpers', runs with this environment passed
/// explicitly: the test threads never touch the process environment.
const GIT_ENV: [(&str, &str); 6] = [
    ("GIT_CONFIG_GLOBAL", "/dev/null"),
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_AUTHOR_NAME", "Test"),
    ("GIT_AUTHOR_EMAIL", "test@example.com"),
    ("GIT_COMMITTER_NAME", "Test"),
    ("GIT_COMMITTER_EMAIL", "test@example.com"),
];

fn git_env() -> Vec<(String, String)> {
    GIT_ENV
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .args(args)
        .envs(GIT_ENV)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?} in {dir:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn write(dir: &Path, file: &str, text: &str) {
    let path = dir.join(file);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn commit_all(dir: &Path, msg: &str) {
    git(dir, &["add", "-A"]);
    git(dir, &["commit", "-q", "-m", msg]);
}

struct World {
    _tmp: tempfile::TempDir,
    remote: PathBuf,
    other: PathBuf,
    clone: PathBuf,
    env: PassEnv,
    spec: CloneSpec,
}

impl World {
    /// A bare remote holding `a.txt` and `b.txt` on `main`; the clone doesn't exist yet.
    fn unsynced() -> World {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().to_path_buf();
        let remote = root.join("remote.git");
        let other = root.join("other");
        fs::create_dir_all(&remote).unwrap();
        git(&remote, &["init", "-q", "--bare", "-b", "main"]);
        git(&root, &["clone", "-q", remote.to_str().unwrap(), "other"]);
        write(&other, "a.txt", "a\n");
        write(&other, "b.txt", "b\n");
        commit_all(&other, "seed");
        git(&other, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
        let clone = root.join("home/git/ctx");
        World {
            remote: remote.clone(),
            other,
            env: PassEnv {
                timeouts: Timeouts::default(),
                git_env: git_env(),
                cancel: None,
            },
            spec: CloneSpec {
                clone_path: clone.clone(),
                remote: remote.to_str().unwrap().to_string(),
                clone_key: "testkey".into(),
            },
            clone,
            _tmp: tmp,
        }
    }

    /// The clone exists and has synced once.
    fn new() -> World {
        let w = World::unsynced();
        assert_eq!(w.pass().outcome, Outcome::Synced);
        w
    }

    fn pass(&self) -> PassResult {
        run_pass(&self.spec, &self.env, &mut |_| {})
    }

    fn pass_observing(&self, observer: &mut dyn FnMut(&StepEvent)) -> PassResult {
        run_pass(&self.spec, &self.env, observer)
    }

    /// A commit from "another machine", pushed.
    fn push_from_other(&self, file: &str, text: &str) {
        git(&self.other, &["pull", "-q", "--ff-only"]);
        write(&self.other, file, text);
        commit_all(&self.other, &format!("remote edit of {file}"));
        git(
            &self.other,
            &["push", "-q", "origin", "HEAD:refs/heads/main"],
        );
    }

    fn remote_main(&self) -> String {
        git(&self.remote, &["rev-parse", "refs/heads/main"])
    }

    fn remote_file(&self, file: &str) -> String {
        git(&self.remote, &["show", &format!("main:{file}")])
    }

    fn read(&self, file: &str) -> String {
        fs::read_to_string(self.clone.join(file)).unwrap()
    }

    fn head(&self) -> String {
        git(&self.clone, &["rev-parse", "HEAD"])
    }
}

fn reason(r: &PassResult) -> (Outcome, Option<Reason>) {
    (r.outcome, r.reason)
}

#[test]
fn local_edit_is_committed_and_pushed() {
    let w = World::new();
    write(&w.clone, "notes/new.md", "hello\n");
    let r = w.pass();
    assert_eq!(reason(&r), (Outcome::Synced, None), "{r:?}");
    assert_eq!(w.remote_file("notes/new.md"), "hello");
    assert!(git(&w.clone, &["log", "-1", "--format=%s"]).starts_with("autosync: "));
    assert_eq!(w.head(), w.remote_main());
    assert_eq!((r.ahead, r.behind), (0, 0));
}

#[test]
fn clean_clone_makes_no_commit() {
    let w = World::new();
    let before = w.head();
    assert_eq!(w.pass().outcome, Outcome::Synced);
    assert_eq!(w.head(), before);
}

#[test]
fn behind_only_fast_forwards_without_a_merge_commit() {
    let w = World::new();
    w.push_from_other("a.txt", "a2\n");
    let r = w.pass();
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    assert_eq!(w.head(), w.remote_main());
    assert_eq!(w.read("a.txt"), "a2\n");
    assert_eq!(
        git(&w.clone, &["log", "-1", "--format=%s"]),
        "remote edit of a.txt"
    );
    assert_eq!(
        git(&w.clone, &["rev-list", "--merges", "--count", "HEAD"]),
        "0"
    );
}

#[test]
fn diverged_clean_merge_is_pushed_with_a_merge_commit() {
    let w = World::new();
    w.push_from_other("a.txt", "remote a\n");
    write(&w.clone, "b.txt", "local b\n");
    let r = w.pass();
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    assert_eq!(w.head(), w.remote_main());
    assert_eq!(
        git(&w.clone, &["rev-list", "--parents", "-n1", "HEAD"])
            .split(' ')
            .count(),
        3
    );
    assert_eq!(
        (w.read("a.txt"), w.read("b.txt")),
        ("remote a\n".into(), "local b\n".into())
    );
    assert_eq!(git(&w.clone, &["status", "--porcelain"]), "");
}

#[test]
fn conflict_leaves_the_worktree_alone_and_clears_once_resolved() {
    let w = World::new();
    w.push_from_other("a.txt", "remote\n");
    let remote_before = w.remote_main();
    write(&w.clone, "a.txt", "local\n");

    let r = w.pass();
    assert_eq!(r.outcome, Outcome::Conflicted, "{r:?}");
    assert_eq!(r.conflict.as_ref().unwrap().paths, ["a.txt"]);
    assert!(r.fix.as_ref().unwrap().contains("merge origin/main"));
    assert_eq!(w.read("a.txt"), "local\n", "no markers in the worktree");
    assert_eq!(git(&w.clone, &["status", "--porcelain"]), "");
    assert!(!w.clone.join(".git/MERGE_HEAD").exists());
    assert_eq!(
        w.remote_main(),
        remote_before,
        "nothing pushed while conflicted"
    );

    // Still conflicted on the next pass, and it keeps committing locally.
    write(&w.clone, "b.txt", "more local\n");
    assert_eq!(w.pass().outcome, Outcome::Conflicted);
    assert_eq!(git(&w.clone, &["status", "--porcelain"]), "");

    // A human resolves it, as the fix says.
    let merge = Command::new("git")
        .args(["merge", "origin/main"])
        .current_dir(&w.clone)
        .envs(GIT_ENV)
        .output()
        .unwrap();
    assert!(!merge.status.success());
    assert!(
        w.clone.join(".git/MERGE_HEAD").exists(),
        "the human's merge must be left open: {}",
        String::from_utf8_lossy(&merge.stderr)
    );
    write(&w.clone, "a.txt", "resolved\n");
    commit_all(&w.clone, "resolve");
    let r = w.pass();
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    assert_eq!(w.remote_file("a.txt"), "resolved");
    assert_eq!(w.remote_file("b.txt"), "more local");
}

/// Writes `file` in the clone the first time a git call named `step` starts, which is
/// after the pass has committed and before it fast-forwards.
fn edit_when(step: &'static str, file: &'static str, clone: PathBuf) -> impl FnMut(&StepEvent) {
    let mut done = false;
    move |ev: &StepEvent| {
        if ev.step == step && !done {
            done = true;
            write(&clone, file, "typed during integrate\n");
        }
    }
}

#[test]
fn edit_during_a_fast_forward_survives() {
    // The count before the fast-forward is the last call that runs before it.
    for (file, blocked) in [("b.txt", false), ("a.txt", true)] {
        let w = World::new();
        w.push_from_other("a.txt", "remote\n");
        let before = w.head();
        let mut observer = edit_when("rev-list", file, w.clone.clone());
        let r = w.pass_observing(&mut observer);
        assert_eq!(w.read(file), "typed during integrate\n", "{file}: {r:?}");
        if blocked {
            assert_eq!(
                reason(&r),
                (Outcome::Failed, Some(Reason::MergeBlockedByDirtyTree))
            );
            assert_eq!(w.head(), before, "nothing moved");
        } else {
            assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
            assert_eq!(w.read("a.txt"), "remote\n");
        }
    }
}

#[test]
fn edit_during_a_merge_commit_fast_forward_survives() {
    for (file, blocked) in [("c.txt", false), ("a.txt", true)] {
        let w = World::new();
        w.push_from_other("a.txt", "remote\n");
        write(&w.clone, "b.txt", "local\n");
        let mut observer = edit_when("commit-tree", file, w.clone.clone());
        let r = w.pass_observing(&mut observer);
        assert_eq!(w.read(file), "typed during integrate\n", "{file}: {r:?}");
        if blocked {
            assert_eq!(
                reason(&r),
                (Outcome::Failed, Some(Reason::MergeBlockedByDirtyTree))
            );
        } else {
            assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
            assert_eq!(w.read("a.txt"), "remote\n");
        }
    }
}

/// A clone on `stray`, two commits ahead of `main` and never pushed.
fn on_stray_branch(w: &World, detached: bool) {
    git(&w.clone, &["checkout", "-q", "-b", "feat/x"]);
    write(&w.clone, "x1.txt", "1\n");
    commit_all(&w.clone, "x1");
    write(&w.clone, "x2.txt", "2\n");
    commit_all(&w.clone, "x2");
    if detached {
        git(&w.clone, &["checkout", "-q", "--detach"]);
    }
}

#[test]
fn a_never_pushed_branch_is_repaired_onto_the_default_branch() {
    for detached in [false, true] {
        let w = World::new();
        on_stray_branch(&w, detached);
        let tip = w.head();
        let r = w.pass();
        assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
        assert_eq!(git(&w.clone, &["symbolic-ref", "HEAD"]), "refs/heads/main");
        assert_eq!(w.remote_main(), tip, "the stray commits were pushed");
        let repair = r.repair.unwrap();
        assert_eq!(repair.commits, 2);
        assert_eq!(
            repair.from,
            if detached {
                "(detached HEAD)"
            } else {
                "feat/x"
            }
        );
        if !detached {
            assert_eq!(
                git(&w.clone, &["rev-parse", "feat/x"]),
                tip,
                "stray ref left alone"
            );
        }
    }
}

#[test]
fn a_commit_between_the_repair_commands_is_committed_on_the_default_branch() {
    let w = World::new();
    on_stray_branch(&w, false);
    let clone = w.clone.clone();
    let mut done = false;
    let mut observer = move |ev: &StepEvent| {
        if ev.step == "update-ref" && !done {
            done = true;
            write(&clone, "between.txt", "made between the two commands\n");
            commit_all(&clone, "between");
        }
    };
    let r = w.pass_observing(&mut observer);
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    assert_eq!(git(&w.clone, &["symbolic-ref", "HEAD"]), "refs/heads/main");
    assert_eq!(w.read("between.txt"), "made between the two commands\n");
    assert_eq!(
        w.remote_file("between.txt"),
        "made between the two commands"
    );
}

#[test]
fn a_pushed_branch_is_not_repaired() {
    let w = World::new();
    on_stray_branch(&w, false);
    git(&w.clone, &["push", "-q", "origin", "feat/x"]);
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Blocked, Some(Reason::OffBranch)),
        "{r:?}"
    );
    assert!(r.fix.unwrap().contains("switch main"));
    assert_eq!(
        git(&w.clone, &["symbolic-ref", "HEAD"]),
        "refs/heads/feat/x"
    );
}

#[test]
fn a_diverged_branch_is_not_repaired() {
    let w = World::new();
    git(&w.clone, &["checkout", "-q", "-b", "feat/x"]);
    git(&w.clone, &["checkout", "-q", "main"]);
    write(&w.clone, "m.txt", "main only\n");
    commit_all(&w.clone, "main moves on");
    git(&w.clone, &["checkout", "-q", "feat/x"]);
    write(&w.clone, "x.txt", "x\n");
    commit_all(&w.clone, "x");
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Blocked, Some(Reason::OffBranch)),
        "{r:?}"
    );
    assert_eq!(
        git(&w.clone, &["symbolic-ref", "HEAD"]),
        "refs/heads/feat/x"
    );
}

#[test]
fn a_default_branch_checked_out_in_a_linked_worktree_is_not_moved() {
    let w = World::new();
    git(&w.clone, &["checkout", "-q", "-b", "feat/x"]);
    let linked = w.clone.with_extension("worktrees");
    git(
        &w.clone,
        &["worktree", "add", "-q", linked.to_str().unwrap(), "main"],
    );
    let main_before = git(&w.clone, &["rev-parse", "main"]);
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Blocked, Some(Reason::OffBranch)),
        "{r:?}"
    );
    assert_eq!(git(&w.clone, &["rev-parse", "main"]), main_before);
}

#[test]
fn an_open_rebase_is_left_alone() {
    let w = World::new();
    w.push_from_other("a.txt", "remote\n");
    write(&w.clone, "a.txt", "local\n");
    commit_all(&w.clone, "local a");
    git(&w.clone, &["fetch", "-q", "origin"]);
    let rebase = Command::new("git")
        .args(["rebase", "origin/main"])
        .current_dir(&w.clone)
        .envs(GIT_ENV)
        .output()
        .unwrap();
    assert!(!rebase.status.success());
    write(&w.clone, "scratch.txt", "mid-rebase\n");
    let (head, remote) = (w.head(), w.remote_main());

    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Blocked, Some(Reason::ForeignOperation)),
        "{r:?}"
    );
    assert_eq!(w.head(), head);
    assert_eq!(w.remote_main(), remote);
    assert!(w.clone.join("scratch.txt").exists());
    assert!(git(&w.clone, &["status", "--porcelain"]).contains("scratch.txt"));
}

#[test]
fn a_clone_path_without_git_leaves_the_parent_repository_alone() {
    let w = World::unsynced();
    let parent = w.clone.parent().unwrap().to_path_buf();
    fs::create_dir_all(&parent).unwrap();
    git(&parent, &["init", "-q", "-b", "main"]);
    write(&parent, "dotfile", "parent\n");
    commit_all(&parent, "parent");
    fs::create_dir_all(&w.clone).unwrap();
    write(&w.clone, "edit.txt", "not in any repository\n");
    let parent_head = git(&parent, &["rev-parse", "HEAD"]);

    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Blocked, Some(Reason::NotARepository)),
        "{r:?}"
    );
    assert_eq!(git(&parent, &["rev-parse", "HEAD"]), parent_head);

    // The ceiling is what stops every other git call from finding that parent.
    let mut quiet = |_: &StepEvent| {};
    let mut g = Git::new(
        &w.clone,
        &parent,
        Timeouts::default(),
        &git_env(),
        &mut quiet,
    );
    let out = g
        .run(
            "rev-parse",
            Duration::from_secs(10),
            &["rev-parse", "--show-toplevel"],
        )
        .unwrap();
    assert!(!out.ok(), "found {}", out.stdout);
}

#[test]
fn a_git_child_past_its_timeout_gets_sigterm_and_leaves_no_index_lock() {
    let w = World::new();
    // `git add` holds `index.lock` while its clean filter runs.
    git(&w.clone, &["config", "filter.slow.clean", "sleep 30; cat"]);
    write(&w.clone, ".git/info/attributes", "*.slow filter=slow\n");
    write(&w.clone, "edit.slow", "x\n");

    let mut env = w.env.clone();
    env.timeouts.add = Duration::from_secs(1);
    let started = Instant::now();
    let mut lock_seen = false;
    let lock = w.clone.join(".git/index.lock");
    let r = run_pass(&w.spec, &env, &mut |ev| {
        if ev.step == "add" {
            std::thread::sleep(Duration::from_millis(300));
            lock_seen = lock.exists();
        }
    });
    assert!(
        lock_seen,
        "the add never held the lock, so the test proves nothing"
    );
    assert!(
        started.elapsed() < Duration::from_secs(20),
        "waited for the filter"
    );
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::CommitFailed)),
        "{r:?}"
    );
    assert!(r.error.unwrap().contains("SIGTERM"));
    assert!(!lock.exists(), "git was killed, not terminated");
}

#[test]
fn an_index_lock_ends_as_index_locked() {
    let w = World::new();
    write(&w.clone, ".git/index.lock", "");
    write(&w.clone, "edit.txt", "x\n");
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::IndexLocked)),
        "{r:?}"
    );
    assert!(r.fix.unwrap().contains(".git/index.lock"));
}

#[test]
fn the_conflict_file_is_never_committed() {
    let w = World::new();
    write(&w.clone, "SYNC-CONFLICT.md", "only this\n");
    let before = w.head();
    assert_eq!(w.pass().outcome, Outcome::Synced);
    assert_eq!(w.head(), before, "a lone SYNC-CONFLICT.md is no change");

    write(&w.clone, "kb/SYNC-CONFLICT.md", "nested\n");
    write(&w.clone, "kb/note.md", "note\n");
    assert_eq!(w.pass().outcome, Outcome::Synced);
    assert_eq!(
        git(&w.remote, &["ls-tree", "-r", "--name-only", "main"])
            .lines()
            .filter(|l| l.contains("SYNC-CONFLICT"))
            .count(),
        0
    );
    assert_eq!(w.remote_file("kb/note.md"), "note");
}

#[test]
fn a_missing_clone_is_cloned_and_a_leftover_temp_clone_removed() {
    let w = World::unsynced();
    let leftover = PathBuf::from(format!("{}.tmp-1", w.clone.display()));
    write(&leftover, "junk", "x");
    let r = w.pass();
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    assert_eq!(w.read("a.txt"), "a\n");
    assert!(!leftover.exists());
}

#[test]
fn a_failed_clone_leaves_nothing_at_the_clone_path() {
    let mut w = World::unsynced();
    w.spec.remote = w.remote.join("missing").to_str().unwrap().to_string();
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::CloneFailed)),
        "{r:?}"
    );
    let parent = w.clone.parent().unwrap();
    assert_eq!(fs::read_dir(parent).unwrap().count(), 0);
}

#[test]
fn an_empty_remote_gets_its_first_commit() {
    let tmp = tempfile::tempdir().unwrap();
    let remote = tmp.path().join("empty.git");
    fs::create_dir_all(&remote).unwrap();
    git(&remote, &["init", "-q", "--bare", "-b", "main"]);
    let clone = tmp.path().join("home/git/ctx");
    let spec = CloneSpec {
        clone_path: clone.clone(),
        remote: remote.to_str().unwrap().into(),
        clone_key: "k".into(),
    };
    let env = PassEnv {
        timeouts: Timeouts::default(),
        git_env: git_env(),
        cancel: None,
    };
    let r = run_pass(&spec, &env, &mut |_| {});
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    write(&clone, "first.md", "first\n");
    let r = run_pass(&spec, &env, &mut |_| {});
    assert_eq!(r.outcome, Outcome::Synced, "{r:?}");
    assert_eq!(git(&remote, &["show", "main:first.md"]), "first");
}

#[test]
fn fetch_and_push_failures_name_their_step_and_keep_the_local_commit() {
    let w = World::new();
    write(&w.clone, "pushme.txt", "x\n");
    write(
        &w.remote,
        "hooks/pre-receive",
        "#!/bin/sh\necho no >&2\nexit 1\n",
    );
    Command::new("chmod")
        .args(["+x", w.remote.join("hooks/pre-receive").to_str().unwrap()])
        .status()
        .unwrap();
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::PushFailed)),
        "{r:?}"
    );
    assert_eq!(r.ahead, 1);
    assert_eq!(git(&w.clone, &["status", "--porcelain"]), "");

    fs::remove_dir_all(&w.remote).unwrap();
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::FetchFailed)),
        "{r:?}"
    );
}

#[test]
fn a_head_that_moves_during_integrate_is_not_merged_over() {
    let w = World::new();
    w.push_from_other("a.txt", "remote\n");
    write(&w.clone, "local.txt", "local\n");
    let clone = w.clone.clone();
    let mut done = false;
    let r = w.pass_observing(&mut |ev| {
        if ev.step == "commit-tree" && !done {
            done = true;
            write(&clone, "a.txt", "human\n");
            git(
                &clone,
                &["commit", "-q", "-a", "-m", "human commit mid-pass"],
            );
        }
    });
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::MergeBlockedByDirtyTree)),
        "{r:?}"
    );
    assert_eq!(w.read("a.txt"), "human\n");
    assert!(!w.clone.join(".git/MERGE_HEAD").exists());
}

#[test]
fn a_default_branch_that_moves_before_the_repair_is_not_overwritten() {
    let w = World::new();
    on_stray_branch(&w, false);
    let clone = w.clone.clone();
    let mut done = false;
    let r = w.pass_observing(&mut |ev| {
        if ev.step == "rev-list" && !done {
            done = true;
            let tree = git(&clone, &["rev-parse", "main^{tree}"]);
            let moved = git(
                &clone,
                &["commit-tree", &tree, "-p", "main", "-m", "main moved"],
            );
            git(&clone, &["update-ref", "refs/heads/main", &moved]);
        }
    });
    assert_eq!(r.outcome, Outcome::Blocked, "{r:?}");
    assert_eq!(r.reason, Some(Reason::OffBranch));
    assert!(r.error.unwrap().contains("expected"));
    assert_eq!(
        git(&w.clone, &["log", "--format=%s", "-1", "main"]),
        "main moved"
    );
}

#[test]
fn a_branch_tracking_another_upstream_is_not_repaired() {
    let w = World::new();
    on_stray_branch(&w, false);
    git(&w.clone, &["config", "branch.feat/x.remote", "origin"]);
    git(
        &w.clone,
        &["config", "branch.feat/x.merge", "refs/heads/other"],
    );
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Blocked, Some(Reason::OffBranch)),
        "{r:?}"
    );
    assert_eq!(
        git(&w.clone, &["symbolic-ref", "HEAD"]),
        "refs/heads/feat/x"
    );
}

#[test]
fn an_incoming_file_does_not_overwrite_an_ignored_local_file() {
    let w = World::new();
    write(&w.clone, ".git/info/exclude", "ignored.txt\n");
    write(&w.clone, "ignored.txt", "mine\n");
    w.push_from_other("ignored.txt", "theirs\n");
    let r = w.pass();
    assert_eq!(
        reason(&r),
        (Outcome::Failed, Some(Reason::MergeBlockedByDirtyTree)),
        "{r:?}"
    );
    assert_eq!(w.read("ignored.txt"), "mine\n");
}

#[test]
fn a_cancelled_pass_starts_no_git_and_commits_nothing() {
    let mut w = World::new();
    write(&w.clone, "new.md", "new\n");
    let head = w.head();
    w.env.cancel = Some(std::sync::Arc::new(std::sync::atomic::AtomicBool::new(
        true,
    )));
    let mut started = Vec::new();
    let r = w.pass_observing(&mut |e| started.push(e.step));
    assert_eq!(reason(&r), (Outcome::Failed, Some(Reason::InternalError)));
    assert!(started.is_empty(), "{started:?}");
    assert_eq!(w.head(), head);
}
