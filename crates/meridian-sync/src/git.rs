//! The git subprocess runner. Every git call in the crate goes through [`Git::run`].
//!
//! git is never SIGKILLed: a git killed that way leaves `index.lock` behind and every
//! later pass fails on it. On a timeout the runner sends SIGTERM, then SIGCONT, to git's
//! process group (so `ssh` and hook children go too, even a stopped one) and keeps
//! waiting for git itself; whoever owns the thread bounds it from outside. It never waits
//! on a grandchild: once git has exited, output still held open by something else is
//! abandoned after a short grace.

use std::ffi::OsString;
use std::io;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// How long output is still read after git has exited, for what a grandchild or the
/// kernel buffer still holds.
const DRAIN_GRACE: Duration = Duration::from_millis(250);
const POLL_INTERVAL_MS: i32 = 25;

/// Pinned on every call: settings that change what a pass does, and the `advice.*` keys
/// that bury the error text in `hint:` lines.
const PINNED_CONFIG: &[&str] = &[
    "merge.autostash=false",
    "commit.gpgsign=false",
    "advice.diverging=false",
    "advice.pushUpdateRejected=false",
    "advice.commitBeforeMerge=false",
    "advice.resolveConflict=false",
    "advice.addIgnoredFile=false",
];

/// Oldest git with `merge-tree --write-tree`.
const MIN_GIT: (u32, u32) = (2, 38);

/// Repository-scoped variables from the environment of whatever started us: left in
/// place they point git at another repository.
const REPO_ENV_VARS: &[&str] = &[
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
];

/// How long each kind of call runs before it is sent SIGTERM.
#[derive(Debug, Clone, Copy)]
pub struct Timeouts {
    pub add: Duration,
    pub commit: Duration,
    pub clone: Duration,
    pub fetch: Duration,
    pub push: Duration,
    pub other: Duration,
}

impl Default for Timeouts {
    fn default() -> Self {
        let min = |m: u64| Duration::from_secs(60 * m);
        Timeouts {
            add: min(10),
            commit: min(10),
            clone: min(10),
            fetch: min(2),
            push: min(2),
            other: min(1),
        }
    }
}

/// A git call that has just started: the worker reports it so a git that never returns
/// can be named.
#[derive(Debug, Clone)]
pub struct StepEvent {
    pub step: &'static str,
    pub pid: u32,
}

#[derive(Debug)]
pub struct Output {
    pub code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.code == Some(0)
    }

    /// What to tell a human when the call failed.
    pub fn error_text(&self) -> String {
        let mut text = self.stderr.trim().to_string();
        if text.is_empty() {
            text = self.stdout.trim().to_string();
        }
        if self.timed_out {
            text = format!("git was sent SIGTERM after its timeout. {text}")
                .trim()
                .to_string();
        }
        if text.is_empty() {
            text = match self.code {
                Some(c) => format!("git exited with status {c}"),
                None => "git was killed by a signal".to_string(),
            };
        }
        text
    }
}

#[derive(Debug)]
pub enum Availability {
    Ok,
    Missing,
    TooOld(String),
}

pub struct Git<'a> {
    cwd: PathBuf,
    ceiling: PathBuf,
    timeouts: Timeouts,
    ssh_command: Option<String>,
    env: Vec<(OsString, OsString)>,
    observer: &'a mut dyn FnMut(&StepEvent),
}

impl<'a> Git<'a> {
    /// Calls run in `cwd`, and git never searches above `ceiling`'s contents: a clone
    /// path that lost its `.git` would otherwise resolve to a repository above it.
    pub fn new(
        cwd: &Path,
        ceiling: &Path,
        timeouts: Timeouts,
        env: &[(String, String)],
        observer: &'a mut dyn FnMut(&StepEvent),
    ) -> Self {
        Git {
            cwd: cwd.to_path_buf(),
            ceiling: ceiling.to_path_buf(),
            timeouts,
            ssh_command: None,
            env: env.iter().map(|(k, v)| (k.into(), v.into())).collect(),
            observer,
        }
    }

    pub fn timeouts(&self) -> Timeouts {
        self.timeouts
    }

    pub fn cwd(&self) -> &Path {
        &self.cwd
    }

    pub fn set_cwd(&mut self, cwd: &Path) {
        self.cwd = cwd.to_path_buf();
    }

    /// Makes ssh fail instead of prompt: appends `-o BatchMode=yes` to the repository's
    /// `core.sshCommand`, else uses plain `ssh`. A `GIT_SSH_COMMAND` the user exported wins.
    pub fn resolve_ssh(&mut self) {
        self.ssh_command = None;
        let exported = std::env::var_os("GIT_SSH_COMMAND").is_some()
            || self.env.iter().any(|(k, _)| k == "GIT_SSH_COMMAND");
        if exported {
            return;
        }
        let configured = self
            .run(
                "config",
                self.timeouts.other,
                &["config", "core.sshCommand"],
            )
            .ok()
            .filter(Output::ok)
            .map(|o| o.stdout.trim().to_string())
            .filter(|s| !s.is_empty());
        let base = configured.unwrap_or_else(|| "ssh".to_string());
        self.ssh_command = Some(format!("{base} -o BatchMode=yes"));
    }

    /// Whether `git` exists and is new enough, from `git --version`.
    pub fn availability(&mut self) -> Availability {
        let out = match self.run_raw("version", self.timeouts.other, &["--version"], false) {
            Ok(out) => out,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Availability::Missing,
            Err(e) => return Availability::TooOld(e.to_string()),
        };
        let text = out.stdout.trim().to_string();
        match parse_version(&text) {
            Some(v) if v >= MIN_GIT => Availability::Ok,
            _ => Availability::TooOld(text),
        }
    }

    /// Runs `git -c <PINNED_CONFIG>... <args>`.
    pub fn run(
        &mut self,
        step: &'static str,
        timeout: Duration,
        args: &[&str],
    ) -> io::Result<Output> {
        self.run_raw(step, timeout, args, true)
    }

    fn run_raw(
        &mut self,
        step: &'static str,
        timeout: Duration,
        args: &[&str],
        pinned: bool,
    ) -> io::Result<Output> {
        let mut cmd = Command::new("git");
        if pinned {
            for setting in PINNED_CONFIG {
                cmd.args(["-c", setting]);
            }
        }
        cmd.args(args).current_dir(&self.cwd);
        for var in REPO_ENV_VARS {
            cmd.env_remove(var);
        }
        cmd.envs(self.env.iter().map(|(k, v)| (k, v)));
        cmd.env("LC_ALL", "C")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_EDITOR", "true")
            .env("GIT_CEILING_DIRECTORIES", &self.ceiling);
        if let Some(ssh) = &self.ssh_command {
            cmd.env("GIT_SSH_COMMAND", ssh);
        }
        cmd.stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);

        let mut child = cmd.spawn()?;
        let pid = child.id();
        (self.observer)(&StepEvent { step, pid });
        let mut out = Pipe::new(child.stdout.take().map(OwnedFd::from));
        let mut err = Pipe::new(child.stderr.take().map(OwnedFd::from));

        let deadline = Instant::now() + timeout;
        let mut timed_out = false;
        let mut exited = None;
        let mut abandon_at = None;
        loop {
            poll_readable(&[&out, &err]);
            out.pump();
            err.pump();
            if exited.is_none() {
                exited = child.try_wait()?;
                if exited.is_some() {
                    abandon_at = Some(Instant::now() + DRAIN_GRACE);
                    out.pump();
                    err.pump();
                }
            }
            if exited.is_none() && !timed_out && Instant::now() >= deadline {
                timed_out = true;
                let group = -(pid as libc::pid_t);
                // SAFETY: `pid` is our own child, and its process group is its pid. A
                // stopped process holds SIGTERM pending until it is continued.
                unsafe {
                    libc::kill(group, libc::SIGTERM);
                    libc::kill(group, libc::SIGCONT);
                }
            }
            if let (Some(_), Some(at)) = (&exited, abandon_at) {
                if (out.fd.is_none() && err.fd.is_none()) || Instant::now() >= at {
                    break;
                }
            }
        }
        Ok(Output {
            code: exited.and_then(|s| s.code()),
            stdout: String::from_utf8_lossy(&out.buf).into_owned(),
            stderr: String::from_utf8_lossy(&err.buf).into_owned(),
            timed_out,
        })
    }
}

/// A non-blocking read end of a child's output; `fd` is `None` once it hit EOF.
struct Pipe {
    fd: Option<OwnedFd>,
    buf: Vec<u8>,
}

impl Pipe {
    fn new(fd: Option<OwnedFd>) -> Pipe {
        if let Some(fd) = &fd {
            // SAFETY: the descriptor is open for as long as `fd` is.
            unsafe {
                let flags = libc::fcntl(fd.as_raw_fd(), libc::F_GETFL);
                libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK);
            }
        }
        Pipe {
            fd,
            buf: Vec::new(),
        }
    }

    /// Reads whatever is available.
    fn pump(&mut self) {
        let Some(raw) = self.fd.as_ref().map(AsRawFd::as_raw_fd) else {
            return;
        };
        let mut chunk = [0u8; 8192];
        loop {
            // SAFETY: `chunk` is valid for `chunk.len()` bytes and `raw` is open.
            let n = unsafe { libc::read(raw, chunk.as_mut_ptr().cast(), chunk.len()) };
            if n > 0 {
                self.buf.extend_from_slice(&chunk[..n as usize]);
                continue;
            }
            if n < 0 {
                match io::Error::last_os_error().kind() {
                    io::ErrorKind::Interrupted => continue,
                    io::ErrorKind::WouldBlock => return,
                    _ => {}
                }
            }
            self.fd = None;
            return;
        }
    }
}

/// Waits up to one interval for output or hang-up on either pipe; with none open it is
/// just the sleep between `try_wait` checks.
fn poll_readable(pipes: &[&Pipe]) {
    let mut fds: Vec<libc::pollfd> = pipes
        .iter()
        .filter_map(|p| p.fd.as_ref())
        .map(|fd| libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        })
        .collect();
    // SAFETY: `fds` is a valid array of `fds.len()` pollfds.
    unsafe {
        libc::poll(
            fds.as_mut_ptr(),
            fds.len() as libc::nfds_t,
            POLL_INTERVAL_MS,
        )
    };
}

fn parse_version(text: &str) -> Option<(u32, u32)> {
    let version = text.strip_prefix("git version ")?;
    let mut parts = version.split(|c: char| !c.is_ascii_digit());
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    Some((major, minor))
}

#[cfg(test)]
mod tests {
    use super::parse_version;

    #[test]
    fn versions_parse() {
        assert_eq!(parse_version("git version 2.43.0"), Some((2, 43)));
        assert_eq!(
            parse_version("git version 2.39.5 (Apple Git-154)"),
            Some((2, 39))
        );
        assert_eq!(parse_version("git version 2.38.0.rc1"), Some((2, 38)));
        assert_eq!(parse_version("nonsense"), None);
    }
}
