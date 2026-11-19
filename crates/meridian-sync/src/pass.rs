//! One sync pass on one clone: preflight, repair, commit, fetch, integrate, push.
//!
//! Integration never touches the worktree except through a fast-forward, which refuses
//! to overwrite a file written since the last commit. `merge-tree` does the merging in
//! the object store.

use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::git::{Availability, Git, Output, StepEvent, Timeouts};
use crate::records::CloneGroup;

/// Excluded from every `git add` and `git status` the pass runs: the daemon writes this
/// file into context directories and it must never be committed. A pathspec needs no
/// edit to the repository, and unlike `.git/info/exclude` it beats an allowlist
/// `.gitignore`.
const CONFLICT_FILE_EXCLUDE: &str = ":(exclude,glob)**/SYNC-CONFLICT.md";

/// Left in `.git` by a person's own operation: the pass touches nothing while one is open.
const FOREIGN_OPERATION_MARKERS: &[&str] = &[
    "MERGE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
];

const ERROR_LIMIT: usize = 500;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Synced,
    Conflicted,
    Blocked,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    OffBranch,
    NotARepository,
    ForeignOperation,
    GitMissing,
    GitTooOld,
    CloneFailed,
    CommitFailed,
    IndexLocked,
    FetchFailed,
    MergeBlockedByDirtyTree,
    PushFailed,
    InternalError,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Synced => "synced",
            Outcome::Conflicted => "conflicted",
            Outcome::Blocked => "blocked",
            Outcome::Failed => "failed",
        }
    }
}

impl Reason {
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::OffBranch => "off_branch",
            Reason::NotARepository => "not_a_repository",
            Reason::ForeignOperation => "foreign_operation",
            Reason::GitMissing => "git_missing",
            Reason::GitTooOld => "git_too_old",
            Reason::CloneFailed => "clone_failed",
            Reason::CommitFailed => "commit_failed",
            Reason::IndexLocked => "index_locked",
            Reason::FetchFailed => "fetch_failed",
            Reason::MergeBlockedByDirtyTree => "merge_blocked_by_dirty_tree",
            Reason::PushFailed => "push_failed",
            Reason::InternalError => "internal_error",
        }
    }

    fn outcome(self) -> Outcome {
        match self {
            Reason::OffBranch
            | Reason::NotARepository
            | Reason::ForeignOperation
            | Reason::GitMissing
            | Reason::GitTooOld => Outcome::Blocked,
            _ => Outcome::Failed,
        }
    }
}

/// What a pass needs to know about a clone.
#[derive(Debug, Clone)]
pub struct CloneSpec {
    pub clone_path: PathBuf,
    pub remote: String,
    pub clone_key: String,
}

impl From<&CloneGroup> for CloneSpec {
    fn from(g: &CloneGroup) -> Self {
        CloneSpec {
            clone_path: g.clone_path.clone(),
            remote: g.remote.clone(),
            clone_key: g.clone_key.clone(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct PassEnv {
    /// The Meridian home: the clone lock lives in `<home>/locks/`.
    pub home: PathBuf,
    pub timeouts: Timeouts,
    /// Variables added to every git call, over the daemon's own environment.
    pub git_env: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Conflict {
    pub paths: Vec<String>,
    pub messages: String,
    pub local_sha: String,
    pub remote_sha: String,
}

/// A stray branch's commits moved onto the default branch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Repair {
    pub from: String,
    pub commits: u64,
    pub at: String,
}

#[derive(Debug, Clone)]
pub struct PassResult {
    pub outcome: Outcome,
    pub reason: Option<Reason>,
    /// Git's error, trimmed.
    pub error: Option<String>,
    /// What a human does about a stop; set for every outcome but `synced`.
    pub fix: Option<String>,
    pub default_branch: Option<String>,
    pub current_branch: Option<String>,
    pub ahead: u64,
    pub behind: u64,
    pub conflict: Option<Conflict>,
    pub repair: Option<Repair>,
}

/// Why a pass ended early.
struct End {
    outcome: Outcome,
    reason: Option<Reason>,
    error: Option<String>,
}

impl End {
    fn new(reason: Reason, error: impl Into<String>) -> Self {
        End {
            outcome: reason.outcome(),
            reason: Some(reason),
            error: Some(trim(error.into())),
        }
    }

    fn bare(reason: Reason) -> Self {
        End {
            outcome: reason.outcome(),
            reason: Some(reason),
            error: None,
        }
    }
}

type Step<T> = Result<T, End>;

/// Runs one full pass on the clone.
pub fn run_pass(
    spec: &CloneSpec,
    env: &PassEnv,
    observer: &mut dyn FnMut(&StepEvent),
) -> PassResult {
    run(spec, env, observer, true)
}

/// Makes sure the clone exists, for a clone whose contexts all have autosync off.
/// `None` means it does; otherwise the result says why not.
pub fn ensure_clone(
    spec: &CloneSpec,
    env: &PassEnv,
    observer: &mut dyn FnMut(&StepEvent),
) -> Option<PassResult> {
    let result = run(spec, env, observer, false);
    (result.outcome != Outcome::Synced).then_some(result)
}

fn run(
    spec: &CloneSpec,
    env: &PassEnv,
    observer: &mut dyn FnMut(&StepEvent),
    full: bool,
) -> PassResult {
    let parent = spec
        .clone_path
        .parent()
        .unwrap_or(Path::new("/"))
        .to_path_buf();
    let mut pass = Pass {
        spec,
        git: Git::new(&parent, &parent, env.timeouts, &env.git_env, observer),
        default_branch: None,
        current_branch: None,
        ahead: 0,
        behind: 0,
        conflict: None,
        repair: None,
    };
    let lock = env
        .home
        .join("locks")
        .join(format!("clone-{}.lock", spec.clone_key));
    let end = match CloneLock::acquire(&lock) {
        Ok(_lock) => pass.execute(full).err(),
        Err(e) => Some(internal(e)),
    };
    pass.finish(end)
}

struct Pass<'a> {
    spec: &'a CloneSpec,
    git: Git<'a>,
    default_branch: Option<String>,
    current_branch: Option<String>,
    ahead: u64,
    behind: u64,
    conflict: Option<Conflict>,
    repair: Option<Repair>,
}

impl Pass<'_> {
    fn execute(&mut self, full: bool) -> Step<()> {
        let parent = self.git.cwd().to_path_buf();
        fs::create_dir_all(&parent).map_err(internal)?;
        match self.git.availability() {
            Availability::Ok => {}
            Availability::Missing => return Err(End::bare(Reason::GitMissing)),
            Availability::TooOld(found) => return Err(End::new(Reason::GitTooOld, found)),
        }
        self.ensure_clone_exists()?;
        if !full {
            return Ok(());
        }
        // From inside the clone now, for its own `core.sshCommand`.
        self.git.resolve_ssh();
        self.reject_foreign_operation()?;
        let b = self.settle_branch()?;
        self.commit_local_changes()?;
        self.fetch(&b)?;
        self.count(&b)?;
        if self.behind > 0 {
            self.integrate(&b)?;
        }
        self.push(&b)
    }

    fn path(&self) -> &Path {
        &self.spec.clone_path
    }

    fn git(
        &mut self,
        step: &'static str,
        timeout: std::time::Duration,
        args: &[&str],
    ) -> Step<Output> {
        self.git.run(step, timeout, args).map_err(internal)
    }

    /// Fast, local, and not expected to fail: a nonzero exit is an `internal_error`.
    fn query(&mut self, step: &'static str, args: &[&str]) -> Step<Output> {
        let timeout = self.git.timeouts().other;
        self.git(step, timeout, args)
    }

    fn query_ok(&mut self, step: &'static str, args: &[&str]) -> Step<String> {
        let out = self.query(step, args)?;
        if out.ok() {
            Ok(out.stdout.trim().to_string())
        } else {
            Err(End::new(Reason::InternalError, out.error_text()))
        }
    }

    /// The object a revision names, or `None`.
    fn resolve(&mut self, rev: &str) -> Step<Option<String>> {
        let out = self.query("rev-parse", &["rev-parse", "--verify", "-q", rev])?;
        Ok(out.ok().then(|| out.stdout.trim().to_string()))
    }

    // ---- 1. Preflight -------------------------------------------------------------

    fn ensure_clone_exists(&mut self) -> Step<()> {
        let path = self.path().to_path_buf();
        let parent = self.git.cwd().to_path_buf();
        let base = path.file_name().map(|n| n.to_string_lossy().into_owned());
        if let (Some(base), Ok(entries)) = (base, fs::read_dir(&parent)) {
            let prefix = format!("{base}.tmp-");
            for entry in entries.filter_map(Result::ok) {
                if entry.file_name().to_string_lossy().starts_with(&prefix) {
                    let _ = fs::remove_dir_all(entry.path());
                }
            }
        }
        if path.symlink_metadata().is_err() {
            // The clone is the call most likely to meet a host-key or passphrase prompt:
            // BatchMode applies to it too, resolved from global config at the parent.
            self.git.resolve_ssh();
            let tmp = PathBuf::from(format!("{}.tmp-{}", path.display(), std::process::id()));
            let tmp_arg = tmp.to_string_lossy().into_owned();
            let remote = self.spec.remote.clone();
            let timeout = self.git.timeouts().clone;
            let cloned = self.git("clone", timeout, &["clone", "--", &remote, &tmp_arg]);
            let result = match cloned {
                Ok(out) if out.ok() => fs::rename(&tmp, &path).map_err(internal),
                Ok(out) => Err(End::new(Reason::CloneFailed, out.error_text())),
                Err(end) => Err(end),
            };
            if result.is_err() {
                let _ = fs::remove_dir_all(&tmp);
            }
            result?;
        } else if !path.join(".git").exists() {
            return Err(End::bare(Reason::NotARepository));
        }
        self.git.set_cwd(&path);
        Ok(())
    }

    fn reject_foreign_operation(&mut self) -> Step<()> {
        let mut args = vec!["rev-parse"];
        for marker in FOREIGN_OPERATION_MARKERS {
            args.push("--git-path");
            args.push(marker);
        }
        let paths = self.query_ok("rev-parse", &args)?;
        let cwd = self.git.cwd().to_path_buf();
        if paths.lines().any(|p| cwd.join(p).exists()) {
            return Err(End::bare(Reason::ForeignOperation));
        }
        Ok(())
    }

    /// The default branch `<b>`, after repairing a stray `HEAD` onto it when that loses
    /// nothing.
    fn settle_branch(&mut self) -> Step<String> {
        let b = self.default_branch()?;
        self.default_branch = Some(b.clone());
        self.current_branch = self.head_branch()?;
        if self.current_branch.as_deref() != Some(&b) {
            self.repair_stray_branch(&b)?;
        }
        Ok(b)
    }

    fn head_branch(&mut self) -> Step<Option<String>> {
        let out = self.query("symbolic-ref", &["symbolic-ref", "-q", "HEAD"])?;
        Ok(out.ok().then(|| {
            out.stdout
                .trim()
                .trim_start_matches("refs/heads/")
                .to_string()
        }))
    }

    /// `origin/HEAD`'s branch, if the clone has one.
    fn origin_head(&mut self) -> Step<Option<String>> {
        let out = self.query(
            "symbolic-ref",
            &["symbolic-ref", "-q", "refs/remotes/origin/HEAD"],
        )?;
        let text = out.stdout.trim();
        Ok(out
            .ok()
            .then(|| {
                text.strip_prefix("refs/remotes/origin/")
                    .map(str::to_string)
            })
            .flatten())
    }

    /// Asks the remote for its `HEAD` and records it as `origin/HEAD`. A failure is not
    /// an error here: an empty remote has no `HEAD` to set.
    fn refresh_origin_head(&mut self) -> Step<()> {
        let timeout = self.git.timeouts().fetch;
        self.git(
            "remote",
            timeout,
            &["remote", "set-head", "origin", "--auto"],
        )?;
        Ok(())
    }

    fn default_branch(&mut self) -> Step<String> {
        if let Some(b) = self.origin_head()? {
            return Ok(b);
        }
        self.refresh_origin_head()?;
        if let Some(b) = self.origin_head()? {
            return Ok(b);
        }
        self.head_branch()?.ok_or_else(|| {
            End::new(
                Reason::OffBranch,
                "HEAD is detached and the remote's default branch is unknown",
            )
        })
    }

    fn repair_stray_branch(&mut self, b: &str) -> Step<()> {
        let stray = self.current_branch.clone();
        let from = stray.clone().unwrap_or_else(|| "(detached HEAD)".into());
        let off_branch = |why: &str| End::new(Reason::OffBranch, why.to_string());

        // 1. Never pushed, and not tracking anything but local or `<b>`.
        if let Some(branch) = &stray {
            if self
                .resolve(&format!("refs/remotes/origin/{branch}"))?
                .is_some()
            {
                return Err(off_branch("the branch exists on the remote"));
            }
            let remote = self.config(&format!("branch.{branch}.remote"))?;
            let merge = self.config(&format!("branch.{branch}.merge"))?;
            let upstream_ok = match (remote.as_deref(), merge.as_deref()) {
                (None, _) | (Some("."), _) => true,
                (Some("origin"), Some(m)) => m == format!("refs/heads/{b}"),
                _ => false,
            };
            if !upstream_ok {
                return Err(off_branch("the branch tracks another upstream"));
            }
        }
        // 2. `<b>` is an ancestor of `HEAD`, so moving it loses nothing.
        let Some(head) = self.resolve("HEAD")? else {
            return Err(off_branch("HEAD has no commits"));
        };
        let Some(old) = self.resolve(&format!("refs/heads/{b}"))? else {
            return Err(off_branch("the default branch does not exist locally"));
        };
        let ancestor = self.query("merge-base", &["merge-base", "--is-ancestor", &old, &head])?;
        if !ancestor.ok() {
            return Err(off_branch("the default branch is not an ancestor of HEAD"));
        }
        // 3. Moving `<b>` would change another worktree's HEAD under its index.
        let worktrees = self.query_ok("worktree", &["worktree", "list", "--porcelain"])?;
        let wanted = format!("branch refs/heads/{b}");
        if worktrees.lines().any(|l| l == wanted) {
            return Err(off_branch(
                "the default branch is checked out in another worktree",
            ));
        }

        let commits = self
            .query_ok(
                "rev-list",
                &["rev-list", "--count", &format!("{old}..{head}")],
            )?
            .parse()
            .unwrap_or(0);
        let target = format!("refs/heads/{b}");
        let moved = self.query(
            "update-ref",
            &[
                "update-ref",
                "-m",
                "meridian-sync: repair",
                &target,
                &head,
                &old,
            ],
        )?;
        if !moved.ok() {
            return Err(End::new(Reason::OffBranch, moved.error_text()));
        }
        // No checkout: the index and worktree stay as they are, so a commit made between
        // the two commands stays staged for the commit step.
        self.query_ok("symbolic-ref", &["symbolic-ref", "HEAD", &target])?;
        self.current_branch = Some(b.to_string());
        self.repair = Some(Repair {
            from,
            commits,
            at: iso_now(),
        });
        Ok(())
    }

    fn config(&mut self, key: &str) -> Step<Option<String>> {
        let out = self.query("config", &["config", "--get", key])?;
        Ok(out.ok().then(|| out.stdout.trim().to_string()))
    }

    // ---- 2. Commit ----------------------------------------------------------------

    fn commit_local_changes(&mut self) -> Step<()> {
        let status = self.query_ok(
            "status",
            &[
                "--no-optional-locks",
                "status",
                "--porcelain",
                "--",
                CONFLICT_FILE_EXCLUDE,
            ],
        )?;
        if status.is_empty() {
            return Ok(());
        }
        let message = format!("autosync: {}", iso_now());
        let timeouts = self.git.timeouts();
        let steps: [(&'static str, std::time::Duration, Vec<&str>); 2] = [
            (
                "add",
                timeouts.add,
                vec!["add", "-A", "--", ".", CONFLICT_FILE_EXCLUDE],
            ),
            ("commit", timeouts.commit, vec!["commit", "-m", &message]),
        ];
        for (step, timeout, args) in steps {
            let out = self.git(step, timeout, &args)?;
            if !out.ok() {
                return Err(self.commit_failure(&out));
            }
        }
        Ok(())
    }

    fn commit_failure(&mut self, out: &Output) -> End {
        let lock = self
            .query_ok("rev-parse", &["rev-parse", "--git-path", "index.lock"])
            .map(|p| self.git.cwd().join(p));
        match lock {
            Ok(lock) if lock.exists() => End::new(Reason::IndexLocked, out.error_text()),
            _ => End::new(Reason::CommitFailed, out.error_text()),
        }
    }

    // ---- 3. Fetch -----------------------------------------------------------------

    /// A remote with no `<b>` yet (an empty remote) is not a failure: there is nothing to
    /// integrate and the push creates it.
    fn fetch(&mut self, b: &str) -> Step<()> {
        let refspec = format!("+refs/heads/{b}:refs/remotes/origin/{b}");
        let timeout = self.git.timeouts().fetch;
        let out = self.git("fetch", timeout, &["fetch", "origin", &refspec])?;
        if out.ok() {
            return Ok(());
        }
        if !out.stderr.contains("couldn't find remote ref") {
            return Err(End::new(Reason::FetchFailed, out.error_text()));
        }
        if self.resolve(&format!("refs/remotes/origin/{b}"))?.is_none() {
            return Ok(());
        }
        // The remote had `<b>` once and no longer does: its default branch was renamed.
        // `set-head --auto` rejects a branch it has no tracking ref for, so fetch first.
        self.git("fetch", timeout, &["fetch", "origin"])?;
        self.refresh_origin_head()?;
        match self.origin_head()? {
            Some(new) if new != b => {
                self.default_branch = Some(new.clone());
                Err(End::new(
                    Reason::OffBranch,
                    format!("the remote's default branch is now {new}, no longer {b}"),
                ))
            }
            _ => {
                let clone = self.path().display().to_string();
                Err(End::new(
                    Reason::FetchFailed,
                    format!(
                        "the remote no longer has {b} and its new default branch is unknown. Run `git -C {clone} remote set-head origin --auto`. git said: {}",
                        out.error_text()
                    ),
                ))
            }
        }
    }

    // ---- 4. Integrate -------------------------------------------------------------

    fn integrate(&mut self, b: &str) -> Step<()> {
        let remote_ref = format!("refs/remotes/origin/{b}");
        let target = if self.ahead == 0 {
            // Behind only: fast-forward, no merge commit, or two machines trade merge
            // commits forever.
            remote_ref
        } else {
            self.merge_commit(&remote_ref)?
        };
        self.fast_forward(&target)?;
        self.count(b)
    }

    /// Merges `remote_ref` into `HEAD` in the object store and returns the merge commit.
    fn merge_commit(&mut self, remote_ref: &str) -> Step<String> {
        let local = self.query_ok("rev-parse", &["rev-parse", "HEAD"])?;
        let remote = self.query_ok("rev-parse", &["rev-parse", remote_ref])?;
        let out = self.query(
            "merge-tree",
            &[
                "merge-tree",
                "--write-tree",
                "--name-only",
                "--messages",
                "HEAD",
                remote_ref,
            ],
        )?;
        let (tree, paths, messages) = parse_merge_tree(&out.stdout);
        match out.code {
            Some(0) => {
                let message = format!(
                    "autosync: merge {}",
                    remote_ref.trim_start_matches("refs/remotes/")
                );
                let commit = self.query(
                    "commit-tree",
                    &[
                        "commit-tree",
                        &tree,
                        "-p",
                        &local,
                        "-p",
                        &remote,
                        "-m",
                        &message,
                    ],
                )?;
                if commit.ok() {
                    Ok(commit.stdout.trim().to_string())
                } else {
                    Err(End::new(Reason::CommitFailed, commit.error_text()))
                }
            }
            Some(1) => {
                self.conflict = Some(Conflict {
                    paths,
                    messages,
                    local_sha: local,
                    remote_sha: remote,
                });
                Err(End {
                    outcome: Outcome::Conflicted,
                    reason: None,
                    error: None,
                })
            }
            _ => Err(End::new(Reason::InternalError, out.error_text())),
        }
    }

    /// The only command that writes the worktree. It refuses to overwrite a file that
    /// changed since the commit step, or to move a `HEAD` that moved.
    fn fast_forward(&mut self, target: &str) -> Step<()> {
        let timeout = self.git.timeouts().other;
        let out = self.git(
            "merge",
            timeout,
            &[
                "merge",
                "--ff-only",
                "--no-autostash",
                "--no-overwrite-ignore",
                target,
            ],
        )?;
        if out.ok() {
            Ok(())
        } else {
            Err(End::new(Reason::MergeBlockedByDirtyTree, out.error_text()))
        }
    }

    // ---- 5. Push ------------------------------------------------------------------

    fn push(&mut self, b: &str) -> Step<()> {
        if self.ahead == 0 {
            return Ok(());
        }
        let refspec = format!("HEAD:refs/heads/{b}");
        let timeout = self.git.timeouts().push;
        let out = self.git("push", timeout, &["push", "origin", &refspec])?;
        if !out.ok() {
            return Err(End::new(Reason::PushFailed, out.error_text()));
        }
        // The push moved `origin/<b>` to `HEAD`.
        self.ahead = 0;
        Ok(())
    }

    /// Commits `HEAD` has that `origin/<b>` lacks, and the reverse.
    fn count(&mut self, b: &str) -> Step<()> {
        let remote_ref = format!("refs/remotes/origin/{b}");
        let head = self.resolve("HEAD")?;
        let remote = self.resolve(&remote_ref)?;
        let counted = |s: &str| s.trim().parse().unwrap_or(0);
        (self.ahead, self.behind) = match (head, remote) {
            (None, None) => (0, 0),
            (Some(_), None) => (
                counted(&self.query_ok("rev-list", &["rev-list", "--count", "HEAD"])?),
                0,
            ),
            (None, Some(_)) => (
                0,
                counted(&self.query_ok("rev-list", &["rev-list", "--count", &remote_ref])?),
            ),
            (Some(_), Some(_)) => {
                let both = format!("HEAD...{remote_ref}");
                let text =
                    self.query_ok("rev-list", &["rev-list", "--left-right", "--count", &both])?;
                let mut n = text.split_whitespace().map(counted);
                (n.next().unwrap_or(0), n.next().unwrap_or(0))
            }
        };
        Ok(())
    }

    // ---- 6. Report ----------------------------------------------------------------

    fn finish(self, end: Option<End>) -> PassResult {
        let (outcome, reason, error) = match end {
            Some(End {
                outcome,
                reason,
                error,
            }) => (outcome, reason, error),
            None => (Outcome::Synced, None, None),
        };
        let mut result = PassResult {
            outcome,
            reason,
            error,
            fix: None,
            default_branch: self.default_branch,
            current_branch: self.current_branch,
            ahead: self.ahead,
            behind: self.behind,
            conflict: self.conflict,
            repair: self.repair,
        };
        result.fix = fix_text(&result, &self.spec.clone_path);
        result
    }
}

fn internal(e: io::Error) -> End {
    End::new(Reason::InternalError, e.to_string())
}

/// What a human does about a stop. `None` for `synced`.
fn fix_text(result: &PassResult, clone: &Path) -> Option<String> {
    let clone = clone.display();
    let b = result
        .default_branch
        .as_deref()
        .unwrap_or("the default branch");
    match (result.outcome, result.reason) {
        (Outcome::Synced, _) => None,
        (Outcome::Conflicted, _) => {
            let paths = result.conflict.as_ref().map(|c| c.paths.join(", ")).unwrap_or_default();
            Some(format!(
                "Conflicting files: {paths}. Run `git -C {clone} merge origin/{b}`, fix the files, commit."
            ))
        }
        (_, Some(Reason::OffBranch)) => {
            let branch = result.current_branch.as_deref().unwrap_or("a detached HEAD");
            Some(format!(
                "{clone} is on {branch}, not {b}. Move its commits to {b} and run `git -C {clone} switch {b}`."
            ))
        }
        (_, Some(Reason::NotARepository)) => Some(format!(
            "{clone} exists but is not a git repository. Move it away; the next pass clones it again."
        )),
        (_, Some(Reason::IndexLocked)) => Some(format!(
            "Delete {clone}/.git/index.lock if no git is running there."
        )),
        (_, Some(Reason::ForeignOperation)) => Some(format!(
            "A merge, rebase, cherry-pick, revert or bisect is open in {clone}. Finish or abort it."
        )),
        (_, Some(Reason::GitMissing)) => Some(
            "git was not found on the daemon's PATH. Install git 2.38 or later where the service can find it."
                .to_string(),
        ),
        (_, Some(Reason::GitTooOld)) => Some(format!(
            "meridian-sync needs git 2.38 or later ({}). Upgrade git.",
            result.error.as_deref().unwrap_or("unknown version")
        )),
        _ => result.error.clone(),
    }
}

/// `merge-tree --write-tree --name-only --messages` prints the tree id, then (when it
/// conflicted) the conflicted paths, a blank line and git's messages.
fn parse_merge_tree(stdout: &str) -> (String, Vec<String>, String) {
    let mut lines = stdout.lines();
    let tree = lines.next().unwrap_or_default().to_string();
    let paths: Vec<String> = lines
        .by_ref()
        .take_while(|l| !l.is_empty())
        .map(str::to_string)
        .collect();
    let messages = lines.collect::<Vec<_>>().join("\n");
    (tree, paths, messages)
}

fn trim(text: String) -> String {
    let text = text.trim();
    match text.char_indices().nth(ERROR_LIMIT) {
        Some((end, _)) => text[..end].to_string(),
        None => text.to_string(),
    }
}

/// `2026-11-19T14:00:00Z`, from the system clock.
fn iso_now() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    iso(secs)
}

fn iso(secs: i64) -> String {
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rem / 3_600,
        rem % 3_600 / 60,
        rem % 60
    )
}

/// An exclusive `flock` on `<home>/locks/clone-<key>.lock`, the lock the `git-autosync`
/// hook takes on the same file. Held until dropped.
struct CloneLock(#[allow(dead_code)] File);

impl CloneLock {
    fn acquire(path: &Path) -> io::Result<CloneLock> {
        loop {
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)?;
            }
            let file = match OpenOptions::new().read(true).write(true).open(path) {
                Ok(f) => f,
                Err(e) if e.kind() == io::ErrorKind::NotFound => {
                    match OpenOptions::new()
                        .read(true)
                        .write(true)
                        .create_new(true)
                        .open(path)
                    {
                        Ok(f) => f,
                        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
                        Err(e) => return Err(e),
                    }
                }
                Err(e) => return Err(e),
            };
            loop {
                // SAFETY: the descriptor is open for as long as `file` is.
                if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0 {
                    break;
                }
                let err = io::Error::last_os_error();
                if err.kind() != io::ErrorKind::Interrupted {
                    return Err(err);
                }
            }
            // The holder before us may have deleted the file; a lock on an unlinked
            // inode excludes nobody.
            let (held, current) = (file.metadata()?, fs::metadata(path));
            if current.is_ok_and(|m| (m.dev(), m.ino()) == (held.dev(), held.ino())) {
                return Ok(CloneLock(file));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_time_formats_known_instants() {
        assert_eq!(iso(0), "1970-01-01T00:00:00Z");
        assert_eq!(iso(951_782_400 + 86_399), "2000-02-29T23:59:59Z");
        assert_eq!(iso(1_795_096_800), "2026-11-19T14:00:00Z");
    }

    #[test]
    fn merge_tree_output_parses() {
        let (tree, paths, messages) =
            parse_merge_tree("abc\na.md\nb.md\n\nCONFLICT (content): x\nmore");
        assert_eq!((tree.as_str(), paths.len()), ("abc", 2));
        assert_eq!(messages, "CONFLICT (content): x\nmore");
        let (tree, paths, _) = parse_merge_tree("abc\n");
        assert_eq!((tree.as_str(), paths.len()), ("abc", 0));
    }
}
