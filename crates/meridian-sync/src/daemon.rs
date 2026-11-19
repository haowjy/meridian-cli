//! The daemon: the lock, one worker per clone, and the main thread that judges every
//! clone and is the only writer of `state.json`.
//!
//! Workers own their clone: they watch it, run its passes, and report two kinds of event
//! (a step started, a pass finished). They write no file outside the clone. The main
//! thread never waits on anything that can block: it reads events with a timeout, and a
//! worker stuck in git is stopped by cancelling the git, not by waiting.

use std::cell::Cell;
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime};

use crate::clock::iso;
use crate::conflict;
use crate::git::{StepEvent, Timeouts};
use crate::pass::{self, CloneSpec, PassEnv, PassResult};
use crate::records::{self, CloneGroup, Scan};
use crate::state::{self, CloneEntry, SkippedRecord, StateFile, Tracker};
use crate::watch::{self, Hint, Watch};

/// Asks to be started again: the daemon's executable was replaced.
pub const EXIT_RESTART: u8 = 75;

const DEBOUNCE: Duration = Duration::from_secs(30);
const DEBOUNCE_FORCE: Duration = Duration::from_secs(5 * 60);
const POLL_INTERVAL: Duration = Duration::from_secs(15);
/// The main loop's wake interval, and so how late it notices anything but an event.
const LOOP_WAIT: Duration = Duration::from_secs(1);
const WRITE_EVERY: Duration = Duration::from_secs(60);
/// How long shutdown waits for workers whose git was sent SIGTERM.
const SHUTDOWN_WAIT: Duration = Duration::from_secs(30);
/// How long a clone polls before native watching is tried again.
const WATCH_RETRY: Duration = Duration::from_secs(5 * 60);
/// How long a notification child may run before it is sent SIGTERM.
const NOTIFY_LIMIT: Duration = Duration::from_secs(60);
/// How long the executable may be missing before it counts as uninstalled: an upgrade
/// that removes the file before writing the new one leaves a gap.
const EXE_GRACE: Duration = Duration::from_secs(5);

static STOP: AtomicBool = AtomicBool::new(false);

pub struct Options {
    pub home: PathBuf,
    /// Block on a busy lock instead of exiting 1.
    pub wait: bool,
}

/// Runs the daemon and returns its exit code: 0 stops for good, 75 asks for a restart,
/// anything else is a failure.
pub fn run(options: Options) -> u8 {
    // A home that is gone is a stop, and nothing here may create it again.
    if !options.home.exists() {
        return 0;
    }
    let autosync_dir = options.home.join("autosync");
    let lock = match take_lock(&autosync_dir.join("daemon.lock"), options.wait) {
        Ok(lock) => lock,
        Err(code) => return code,
    };
    install_signal_handlers();
    detach_from_terminal();
    for (key, _) in std::env::vars_os() {
        let key = key.to_string_lossy();
        // The daemon's `meridian notify` must see only file-configured settings, as a service
        // would, whatever shell started a foreground run.
        if (key.starts_with("MERIDIAN_") && key != "MERIDIAN_HOME") || key.starts_with("_MERIDIAN_")
        {
            std::env::remove_var(&*key);
        }
    }
    let code = match Daemon::new(options.home, &autosync_dir, &lock) {
        Ok(daemon) => daemon.run(),
        Err(e) => {
            eprintln!("meridian-sync: {e}");
            1
        }
    };
    drop(lock);
    code
}

/// An exclusive `flock` on `daemon.lock`, held until exit: the only liveness signal.
/// The kernel frees it when the process dies, however it dies.
fn take_lock(path: &Path, wait: bool) -> Result<File, u8> {
    let fail = |e: std::io::Error| {
        eprintln!("meridian-sync: {}: {e}", path.display());
        1
    };
    fs::create_dir_all(path.parent().unwrap_or(Path::new("."))).map_err(fail)?;
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(fail)?;
    let flags = if wait {
        libc::LOCK_EX
    } else {
        libc::LOCK_EX | libc::LOCK_NB
    };
    loop {
        // SAFETY: the descriptor is open for as long as `file` is.
        if unsafe { libc::flock(file.as_raw_fd(), flags) } == 0 {
            break;
        }
        let err = std::io::Error::last_os_error();
        match err.kind() {
            std::io::ErrorKind::Interrupted => continue,
            std::io::ErrorKind::WouldBlock => {
                let mut pid = String::new();
                let _ = file.read_to_string(&mut pid);
                eprintln!("already running (pid {})", pid.trim());
                return Err(1);
            }
            _ => return Err(fail(err)),
        }
    }
    file.set_len(0).map_err(fail)?;
    write!(file, "{}", std::process::id()).map_err(fail)?;
    Ok(file)
}

extern "C" fn on_signal(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}

/// Only after the lock is held, so SIGTERM ends a waiting instance at once.
fn install_signal_handlers() {
    for sig in [libc::SIGTERM, libc::SIGINT] {
        // SAFETY: the handler only stores to an atomic.
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = on_signal as *const () as usize;
            libc::sigemptyset(&mut action.sa_mask);
            libc::sigaction(sig, &action, std::ptr::null_mut());
        }
    }
}

/// A git in a background process group that reads the terminal gets stopped. With no
/// controlling terminal there is nothing to read. Fails harmlessly for a process that
/// already leads its session (a service manager's child).
fn detach_from_terminal() {
    // SAFETY: no arguments, no memory.
    unsafe { libc::setsid() };
}

// ---- Workers -----------------------------------------------------------------------

/// What the main thread knows of a clone: everything a worker needs to run its passes.
#[derive(Debug, Clone, PartialEq, Eq)]
struct WorkerConfig {
    clone_path: PathBuf,
    clone_key: String,
    remote: String,
    autosync: bool,
    /// The context directories of the clone, where `SYNC-CONFLICT.md` goes.
    dirs: Vec<conflict::Dir>,
    /// The newest root-record `written_at` read so far; a change asks for a pass.
    written_at: String,
}

impl From<&CloneGroup> for WorkerConfig {
    fn from(g: &CloneGroup) -> Self {
        WorkerConfig {
            clone_path: g.clone_path.clone(),
            clone_key: g.clone_key.clone(),
            remote: g.remote.clone(),
            autosync: g.autosync,
            dirs: conflict::dirs(&g.contexts),
            written_at: g.written_at.clone(),
        }
    }
}

enum Msg {
    Config(WorkerConfig),
    /// A pass now: a tick, a retry.
    Pass,
    Watch(Hint),
    Stop,
}

enum Event {
    Step {
        key: String,
        step: &'static str,
        pid: u32,
    },
    Pass {
        key: String,
        /// The `written_at` the worker had read when the pass started.
        answered: String,
        /// `None`: an off clone that exists.
        result: Option<Box<PassResult>>,
    },
    Watch {
        key: String,
        mode: &'static str,
    },
}

struct Worker {
    cfg: WorkerConfig,
    events: Sender<Event>,
    rx: Receiver<Msg>,
    tx: Sender<Msg>,
    cancel: Arc<AtomicBool>,
    watch: Option<Watch>,
    polling: bool,
    /// When polling began, so a clone that shrank gets its watches back.
    polling_since: Option<Instant>,
    /// First and last file event since the last pass.
    dirty: Option<(Instant, Instant)>,
    /// Why the next pass runs, once one is wanted.
    pass_wanted: Option<&'static str>,
}

impl Worker {
    fn run(mut self) {
        self.pass_wanted = Some("start");
        let mut last_poll = Instant::now();
        while !self.cancel.load(Ordering::Relaxed) {
            let now = Instant::now();
            let debounced = self.dirty.is_some_and(|(first, last)| {
                now - last >= DEBOUNCE || now - first >= DEBOUNCE_FORCE
            });
            if let Some(why) = self.pass_wanted.or(debounced.then_some("file change")) {
                self.pass_wanted = None;
                self.dirty = None;
                self.pass(why);
                self.ensure_watch();
                last_poll = Instant::now();
                continue;
            }
            let wait = match self.dirty {
                Some((first, last)) => (last + DEBOUNCE)
                    .min(first + DEBOUNCE_FORCE)
                    .saturating_duration_since(now),
                None => LOOP_WAIT,
            }
            .clamp(Duration::from_millis(10), LOOP_WAIT);
            match self.rx.recv_timeout(wait) {
                Ok(msg) => {
                    if !self.handle(msg) {
                        return;
                    }
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => return,
            }
            if self.polling && self.cfg.autosync && last_poll.elapsed() >= POLL_INTERVAL {
                last_poll = Instant::now();
                if self.has_changes() {
                    self.mark_dirty();
                }
            }
        }
    }

    /// False when the worker should end.
    fn handle(&mut self, msg: Msg) -> bool {
        match msg {
            Msg::Stop => return false,
            Msg::Pass => self.pass_wanted = Some("requested"),
            Msg::Config(cfg) => {
                self.pass_wanted = Some("record");
                if !cfg.autosync {
                    self.watch = None;
                    self.dirty = None;
                }
                self.cfg = cfg;
            }
            Msg::Watch(hint) => match hint {
                Hint::Change => self.mark_dirty(),
                Hint::Head => self.pass_wanted = Some("HEAD moved"),
                Hint::Rescan => {
                    if self.watch.as_mut().is_some_and(|w| w.rewalk().is_err()) {
                        self.fall_back_to_polling();
                    }
                    self.pass_wanted = Some("events lost");
                }
                Hint::NewDir(dir) => {
                    if self
                        .watch
                        .as_mut()
                        .is_some_and(|w| w.add_tree(&dir).is_err())
                    {
                        self.fall_back_to_polling();
                    }
                }
                Hint::Failed => {
                    self.fall_back_to_polling();
                    self.pass_wanted = Some("watch failed");
                }
            },
        }
        true
    }

    fn mark_dirty(&mut self) {
        let now = Instant::now();
        self.dirty = Some(self.dirty.map_or((now, now), |(first, _)| (first, now)));
    }

    fn spec(&self) -> CloneSpec {
        CloneSpec {
            clone_path: self.cfg.clone_path.clone(),
            remote: self.cfg.remote.clone(),
            clone_key: self.cfg.clone_key.clone(),
        }
    }

    fn pass_env(&self) -> PassEnv {
        PassEnv {
            timeouts: Timeouts::default(),
            git_env: Vec::new(),
            cancel: Some(Arc::clone(&self.cancel)),
        }
    }

    fn has_changes(&self) -> bool {
        pass::has_local_changes(&self.spec(), &self.pass_env(), &mut |_| {})
    }

    fn pass(&mut self, why: &str) {
        let (spec, env) = (self.spec(), self.pass_env());
        let answered = self.cfg.written_at.clone();
        let key = self.cfg.clone_key.clone();
        let events = self.events.clone();
        let acted = Cell::new(false);
        let mut observe = |e: &StepEvent| {
            if matches!(e.step, "commit" | "merge" | "merge-tree" | "push" | "clone") {
                acted.set(true);
            }
            let _ = events.send(Event::Step {
                key: key.clone(),
                step: e.step,
                pid: e.pid,
            });
        };
        let ran = catch_unwind(AssertUnwindSafe(|| {
            if self.cfg.autosync {
                Some(pass::run_pass(&spec, &env, &mut observe))
            } else {
                pass::ensure_clone(&spec, &env, &mut observe)
            }
        }));
        let result = ran.unwrap_or_else(|panic| {
            let text = panic
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| panic.downcast_ref::<String>().cloned())
                .unwrap_or_default();
            Some(PassResult::internal_error(
                &format!("the worker panicked: {text}"),
                &spec.clone_path,
            ))
        });
        // A pass cut short by shutdown isn't the clone's state.
        if !self.cancel.load(Ordering::Relaxed) {
            if let Some(r) = result.as_ref() {
                // Only a conflict or a sync changes the file: a failed fetch says nothing
                // about whether the conflict is still there.
                match r.outcome {
                    pass::Outcome::Conflicted => {
                        conflict::sync(&spec.clone_path, &self.cfg.dirs, r.conflict.as_ref());
                    }
                    pass::Outcome::Synced => conflict::sync(&spec.clone_path, &self.cfg.dirs, None),
                    _ => {}
                }
                if acted.get() || r.outcome != pass::Outcome::Synced || r.repair.is_some() {
                    eprintln!(
                        "meridian-sync: pass on {} ({why}): {:?}{}",
                        spec.clone_path.display(),
                        r.outcome,
                        r.reason.map(|r| format!(" {r:?}")).unwrap_or_default()
                    );
                }
            }
            let _ = self.events.send(Event::Pass {
                key,
                answered,
                result: result.map(Box::new),
            });
        }
    }

    fn ensure_watch(&mut self) {
        if self
            .polling_since
            .is_some_and(|t| t.elapsed() >= WATCH_RETRY)
        {
            self.polling = false;
        }
        if !self.cfg.autosync
            || self.watch.is_some()
            || self.polling
            || !self.cfg.clone_path.join(".git").is_dir()
        {
            return;
        }
        let tx = self.tx.clone();
        let sink: watch::Sink = Arc::new(move |hint| {
            let _ = tx.send(Msg::Watch(hint));
        });
        match watch::start(&self.cfg.clone_path, sink) {
            Some(w) => {
                self.watch = Some(w);
                self.report_mode("native");
            }
            None => self.fall_back_to_polling(),
        }
    }

    fn fall_back_to_polling(&mut self) {
        self.watch = None;
        self.polling = true;
        self.polling_since = Some(Instant::now());
        self.report_mode("polling");
    }

    fn report_mode(&self, mode: &'static str) {
        let _ = self.events.send(Event::Watch {
            key: self.cfg.clone_key.clone(),
            mode,
        });
    }
}

struct WorkerHandle {
    key: String,
    tx: Sender<Msg>,
    join: JoinHandle<()>,
    cancel: Arc<AtomicBool>,
}

impl WorkerHandle {
    fn stop(&self) {
        self.cancel.store(true, Ordering::Relaxed);
        let _ = self.tx.send(Msg::Stop);
    }
}

/// Everything the main thread holds for one clone.
struct Slot {
    tracker: Tracker,
    /// What the worker was last told.
    sent: WorkerConfig,
    worker: Option<WorkerHandle>,
}

// ---- The main thread ---------------------------------------------------------------

/// Identity of the running executable: a replaced binary is a new `(dev, inode, mtime)`.
type ExeStamp = (u64, u64, i64);

/// `(dev, inode)` of a file: the lock is the file this identifies, not the path.
type FileId = (u64, u64);

fn file_id(m: &fs::Metadata) -> FileId {
    (m.dev(), m.ino())
}

fn exe_stamp(path: &Path) -> Option<ExeStamp> {
    let m = fs::metadata(path).ok()?;
    Some((m.dev(), m.ino(), m.mtime()))
}

struct Daemon {
    home: PathBuf,
    lock_path: PathBuf,
    /// The lock file this daemon holds; another file at `lock_path` means a second daemon
    /// could hold the lock too.
    lock_id: FileId,
    exe_missing_since: Option<Instant>,
    roots_dir: PathBuf,
    state_path: PathBuf,
    exe: Option<(PathBuf, ExeStamp)>,
    started_at: String,
    previous: BTreeMap<String, CloneEntry>,
    slots: BTreeMap<String, Slot>,
    retiring: Vec<WorkerHandle>,
    scan: Scan,
    scanned_at: String,
    events_tx: Sender<Event>,
    events_rx: Receiver<Event>,
    /// `meridian notify` children, each with its start and whether it was sent SIGTERM.
    notifications: Vec<(Child, Instant, bool)>,
    dirty: bool,
    last_write: Option<Instant>,
}

impl Daemon {
    fn new(home: PathBuf, autosync_dir: &Path, lock: &File) -> Result<Daemon, String> {
        let lock_id = file_id(&lock.metadata().map_err(|e| e.to_string())?);
        let (events_tx, events_rx) = mpsc::channel();
        let state_path = autosync_dir.join("state.json");
        let exe = std::env::current_exe()
            .ok()
            .and_then(|p| exe_stamp(&p).map(|s| (p, s)));
        Ok(Daemon {
            lock_path: autosync_dir.join("daemon.lock"),
            lock_id,
            exe_missing_since: None,
            roots_dir: autosync_dir.join("roots"),
            previous: state::read_previous(&state_path),
            state_path,
            home,
            exe,
            started_at: iso(SystemTime::now()),
            slots: BTreeMap::new(),
            retiring: Vec::new(),
            scan: Scan::default(),
            scanned_at: String::new(),
            events_tx,
            events_rx,
            notifications: Vec::new(),
            dirty: true,
            last_write: None,
        })
    }

    fn run(mut self) -> u8 {
        let code = loop {
            if let Some(code) = self.exit_reason() {
                break code;
            }
            self.refresh();
            if let Ok(event) = self.events_rx.recv_timeout(LOOP_WAIT) {
                self.apply(event);
                while let Ok(event) = self.events_rx.try_recv() {
                    self.apply(event);
                }
            }
        };
        self.shutdown(code);
        code
    }

    /// `0` stops for good, `75` asks for a restart.
    fn exit_reason(&mut self) -> Option<u8> {
        if STOP.load(Ordering::Relaxed) || !self.home.exists() {
            return Some(0);
        }
        // `autosync/` was deleted or replaced under us: a new daemon may hold the lock.
        if fs::metadata(&self.lock_path).map(|m| file_id(&m)).ok() != Some(self.lock_id) {
            return Some(EXIT_RESTART);
        }
        let (path, stamp) = self.exe.as_ref()?;
        match exe_stamp(path) {
            // Still missing after the grace: meridian-cli was uninstalled, and nothing
            // will ask for a restart.
            None => {
                let since = *self.exe_missing_since.get_or_insert_with(Instant::now);
                (since.elapsed() >= EXE_GRACE).then_some(0)
            }
            Some(now) => {
                self.exe_missing_since = None;
                (now != *stamp).then_some(EXIT_RESTART)
            }
        }
    }

    /// Lists the root records, starts and stops workers, hands out triggers, judges every
    /// clone and writes the state file when it changed or a minute has passed.
    fn refresh(&mut self) {
        let (now, wall) = (Instant::now(), SystemTime::now());
        let scan = records::scan(&self.roots_dir);
        if scan != self.scan {
            self.scan = scan;
            self.dirty = true;
        }
        self.scanned_at = iso(wall);
        self.reconcile(now, wall);
        for slot in self.slots.values_mut() {
            if slot.worker.as_ref().is_some_and(|w| w.join.is_finished()) {
                // The worker's thread ended without a result: its pass failed.
                slot.worker = None;
                slot.tracker.worker_died();
                let result =
                    PassResult::internal_error("the worker thread ended", &slot.sent.clone_path);
                slot.tracker
                    .on_pass(slot.sent.written_at.clone(), Some(result), now, wall);
                self.dirty = true;
            }
        }
        self.retiring.retain(|w| !w.join.is_finished());
        let mut stopped = Vec::new();
        for (key, slot) in &mut self.slots {
            if let Some(why) = slot.tracker.judge(now, wall) {
                eprintln!(
                    "meridian-sync: {} stopped: {}",
                    slot.sent.clone_path.display(),
                    why.0
                );
                stopped.push((key.clone(), why.0, slot.tracker.entry().fix.clone()));
                self.dirty = true;
            }
        }
        for (key, why, fix) in stopped {
            self.notify_stopped(&key, &why, fix);
        }
        self.reap_notifications(now);
        if self.dirty || self.last_write.is_none_or(|t| now - t >= WRITE_EVERY) {
            self.write(now);
        }
    }

    /// Runs `meridian -C <root> notify` for a clone that just stopped, where `<root>` is the
    /// project of the newest record naming it: that project's routes and the user's apply.
    /// Never waits on the child; `reap_notifications` checks on it each loop.
    fn notify_stopped(&mut self, key: &str, why: &str, fix: Option<String>) {
        let Some(group) = self.scan.clones.iter().find(|g| g.clone_key == key) else {
            return;
        };
        let mut names: Vec<&str> = group
            .contexts
            .iter()
            .filter(|c| c.root == group.newest_root)
            .map(|c| c.name.as_str())
            .collect();
        names.dedup();
        let title = format!("autosync stopped: {}", names.join(", "));
        let body = fix.unwrap_or_else(|| why.to_string());
        let Some(meridian) = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.join("meridian")))
        else {
            return;
        };
        let child = Command::new(&meridian)
            .arg("-C")
            .arg(&group.newest_root)
            .args(["notify", &title, &body, "--priority", "4"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .spawn();
        match child {
            Ok(child) => self.notifications.push((child, Instant::now(), false)),
            Err(e) => eprintln!("meridian-sync: cannot run {}: {e}", meridian.display()),
        }
    }

    /// Collects finished notification children and sends SIGTERM to one past its limit.
    fn reap_notifications(&mut self, now: Instant) {
        self.notifications.retain_mut(|(child, started, termed)| {
            match child.try_wait() {
                Ok(Some(status)) => {
                    if !status.success() {
                        eprintln!("meridian-sync: notification failed ({status})");
                    }
                    false
                }
                Ok(None) => {
                    if !*termed && now.saturating_duration_since(*started) >= NOTIFY_LIMIT {
                        // SAFETY: a signal to a child this process started and hasn't reaped.
                        unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
                        *termed = true;
                    }
                    true
                }
                Err(_) => false,
            }
        });
    }

    fn reconcile(&mut self, now: Instant, wall: SystemTime) {
        let named: Vec<&CloneGroup> = self.scan.clones.iter().collect();
        let gone: Vec<String> = self
            .slots
            .keys()
            .filter(|k| !named.iter().any(|g| &g.clone_key == *k))
            .cloned()
            .collect();
        for key in gone {
            if let Some(slot) = self.slots.remove(&key) {
                if let Some(w) = slot.worker {
                    w.stop();
                    self.retiring.push(w);
                }
            }
            self.dirty = true;
        }
        for group in named {
            let cfg = WorkerConfig::from(group);
            let slot = self.slots.entry(cfg.clone_key.clone()).or_insert_with(|| {
                let tracker = match self.previous.remove(&cfg.clone_key) {
                    Some(prev) => Tracker::restore(prev, cfg.autosync, now),
                    None => Tracker::new(cfg.clone_path.clone(), cfg.autosync, now),
                };
                Slot {
                    tracker,
                    // Nothing sent yet: the first config below starts the worker.
                    sent: WorkerConfig {
                        written_at: String::new(),
                        ..cfg.clone()
                    },
                    worker: None,
                }
            });
            let changed = slot.sent != cfg;
            slot.tracker.set_autosync(cfg.autosync, now);
            let due = slot.tracker.due(now, wall);
            match &slot.worker {
                Some(w) => {
                    if changed {
                        let _ = w.tx.send(Msg::Config(cfg.clone()));
                        slot.tracker.requested();
                    } else if due.is_some() {
                        let _ = w.tx.send(Msg::Pass);
                        slot.tracker.requested();
                    }
                }
                // A worker that ended is started again on the clone's next trigger.
                // Not while the old worker for the key is still ending: two workers in one
                // clone.
                None if (changed || due.is_some())
                    && !self.retiring.iter().any(|w| w.key == cfg.clone_key) =>
                {
                    slot.worker = Some(spawn_worker(&cfg, &self.events_tx));
                    slot.tracker.requested();
                }
                None => {}
            }
            if changed {
                slot.sent = cfg;
                self.dirty = true;
            }
        }
    }

    fn apply(&mut self, event: Event) {
        let (now, wall) = (Instant::now(), SystemTime::now());
        match event {
            Event::Step { key, step, pid } => {
                if let Some(slot) = self.slots.get_mut(&key) {
                    slot.tracker.on_step(step, pid, now, wall);
                    self.dirty = true;
                }
            }
            Event::Pass {
                key,
                answered,
                result,
            } => {
                if let Some(slot) = self.slots.get_mut(&key) {
                    slot.tracker
                        .on_pass(answered, result.map(|r| *r), now, wall);
                    self.dirty = true;
                }
            }
            Event::Watch { key, mode } => {
                if let Some(slot) = self.slots.get_mut(&key) {
                    slot.tracker.set_watch(mode);
                    self.dirty = true;
                }
            }
        }
    }

    fn write(&mut self, now: Instant) {
        let file = StateFile {
            v: state::FORMAT_V,
            pid: std::process::id(),
            version: crate::version(),
            started_at: self.started_at.clone(),
            alive_at: iso(SystemTime::now()),
            scanned_at: self.scanned_at.clone(),
            skipped_records: self.scan.skipped.iter().map(SkippedRecord::from).collect(),
            clones: self
                .slots
                .iter()
                .map(|(k, s)| (k.clone(), s.tracker.entry().clone()))
                .collect(),
        };
        if let Err(e) = state::write(&self.state_path, &file) {
            eprintln!(
                "meridian-sync: cannot write {}: {e}",
                self.state_path.display()
            );
        }
        // A failed write is tried again at the next minute, not on every loop.
        self.dirty = false;
        self.last_write = Some(now);
    }

    /// Every running git gets SIGTERM (through its worker's cancel flag) and is waited
    /// for, so git removes its own lock; the last state is written for the next start.
    fn shutdown(mut self, code: u8) {
        let mut workers: Vec<WorkerHandle> = self
            .slots
            .values_mut()
            .filter_map(|s| s.worker.take())
            .collect();
        workers.append(&mut self.retiring);
        for w in &workers {
            w.stop();
        }
        let deadline = Instant::now() + SHUTDOWN_WAIT;
        for w in &workers {
            while !w.join.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(25));
            }
        }
        // A worker that ended took its git with it: no step is left running. One that
        // didn't (a git that ignored SIGTERM) keeps its step for the next daemon.
        for w in workers.iter().filter(|w| w.join.is_finished()) {
            if let Some(slot) = self.slots.get_mut(&w.key) {
                slot.tracker.step_ended();
            }
        }
        if code == EXIT_RESTART || (code == 0 && self.home.exists()) {
            let now = Instant::now();
            let wall = SystemTime::now();
            for slot in self.slots.values_mut() {
                slot.tracker.judge(now, wall);
            }
            self.write(now);
        }
    }
}

fn spawn_worker(cfg: &WorkerConfig, events: &Sender<Event>) -> WorkerHandle {
    let (tx, rx) = mpsc::channel();
    let cancel = Arc::new(AtomicBool::new(false));
    let worker = Worker {
        cfg: cfg.clone(),
        events: events.clone(),
        rx,
        tx: tx.clone(),
        cancel: Arc::clone(&cancel),
        watch: None,
        polling: false,
        polling_since: None,
        dirty: None,
        pass_wanted: None,
    };
    let join = thread::Builder::new()
        .name(format!("worker-{}", cfg.clone_key))
        .spawn(move || worker.run())
        .expect("spawn a worker thread");
    WorkerHandle {
        key: cfg.clone_key.clone(),
        tx,
        join,
        cancel,
    }
}
