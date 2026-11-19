//! `state.json`: the model, its one writer, and the judge that decides each clone's
//! state from the events its worker reports.
//!
//! Every duration the rules use is an `Instant` (it doesn't advance while the machine is
//! suspended, so a laptop that slept overnight doesn't alarm on wake). The times in the
//! file are wall-clock and for display; the two `_for_s` durations are the judge's own
//! timers, written so a restart can continue them.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use serde::{Deserialize, Serialize};

use crate::clock::iso;
use crate::pass::{Outcome, PassResult};
use crate::records::Skipped;

/// The only `state.json` format this build reads and writes.
pub const FORMAT_V: i64 = 1;

/// A clone is stopped after this long unsuccessful, or with no worker report.
pub const STOP_AFTER: Duration = Duration::from_secs(15 * 60);
/// A pass runs at least this often, as the fetch cadence.
pub const TICK: Duration = Duration::from_secs(5 * 60);
const RETRY_STEP: Duration = Duration::from_secs(60);
const RETRIES: u32 = 2;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Off,
    Pending,
    Synced,
    Retrying,
    Stopped,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConflictEntry {
    pub paths: Vec<String>,
    pub messages: String,
    pub local_sha: String,
    pub remote_sha: String,
    pub since: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RepairEntry {
    pub from: String,
    pub commits: u64,
    pub at: String,
}

/// One clone's entry in `state.json`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloneEntry {
    pub clone_path: PathBuf,
    pub state: State,
    #[serde(default)]
    pub stopped_since: Option<String>,
    #[serde(default)]
    pub outcome: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub fix: Option<String>,
    #[serde(default)]
    pub default_branch: Option<String>,
    #[serde(default)]
    pub current_branch: Option<String>,
    #[serde(default)]
    pub ahead: u64,
    #[serde(default)]
    pub behind: u64,
    #[serde(default)]
    pub last_pass_at: Option<String>,
    #[serde(default)]
    pub last_success_at: Option<String>,
    /// The newest root-record `written_at` the daemon had read when the last finished
    /// pass started.
    #[serde(default)]
    pub answered: Option<String>,
    #[serde(default)]
    pub step: Option<String>,
    #[serde(default)]
    pub step_pid: Option<u32>,
    #[serde(default)]
    pub step_started_at: Option<String>,
    #[serde(default)]
    pub unsuccessful_for_s: u64,
    #[serde(default)]
    pub quiet_for_s: u64,
    #[serde(default)]
    pub conflict: Option<ConflictEntry>,
    #[serde(default)]
    pub last_repair: Option<RepairEntry>,
    #[serde(default = "native")]
    pub watch: String,
}

fn native() -> String {
    "native".to_string()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SkippedRecord {
    pub file: String,
    pub v: Option<i64>,
    pub error: String,
}

impl From<&Skipped> for SkippedRecord {
    fn from(s: &Skipped) -> Self {
        SkippedRecord {
            file: s.file.clone(),
            v: s.v,
            error: s.error.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateFile {
    pub v: i64,
    pub pid: u32,
    pub version: String,
    pub started_at: String,
    pub alive_at: String,
    pub scanned_at: String,
    #[serde(default)]
    pub skipped_records: Vec<SkippedRecord>,
    #[serde(default)]
    pub clones: BTreeMap<String, CloneEntry>,
}

/// The previous daemon's file, for restoring timers. A file that is missing, torn or of
/// another `v` is ignored: every clone then starts `pending`.
pub fn read_previous(path: &Path) -> BTreeMap<String, CloneEntry> {
    let Ok(text) = fs::read_to_string(path) else {
        return BTreeMap::new();
    };
    match serde_json::from_str::<StateFile>(&text) {
        Ok(file) if file.v == FORMAT_V => file.clones,
        _ => BTreeMap::new(),
    }
}

/// Writes the file atomically: a reader sees the old file or the new one.
pub fn write(path: &Path, file: &StateFile) -> std::io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy())
        .unwrap_or_default();
    let tmp = dir.join(format!(".{name}.tmp-{}", std::process::id()));
    let mut out = fs::File::create(&tmp)?;
    out.write_all(&serde_json::to_vec_pretty(file).map_err(std::io::Error::other)?)?;
    drop(out);
    fs::rename(&tmp, path).inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

/// Why a clone was newly judged stopped, for the log (and C's notification).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BecameStopped(pub String);

/// One clone's entry plus the timers behind its state.
pub struct Tracker {
    entry: CloneEntry,
    autosync: bool,
    /// Start of the current unbroken run of unsuccessful passes: when its first finished.
    unsuccessful_since: Option<Instant>,
    /// The last step started or pass finished.
    last_progress: Instant,
    /// Restored mid-pass. A step started by the restarted worker is not progress until a
    /// pass finishes, so a restart loop around a git that never returns still reaches
    /// the no-progress stop.
    carried: bool,
    /// A pass has been asked for or is running.
    in_flight: bool,
    failures: u32,
    last_pass_end: Option<(Instant, SystemTime)>,
}

impl Tracker {
    pub fn new(clone_path: PathBuf, autosync: bool, now: Instant) -> Self {
        Tracker {
            entry: CloneEntry {
                clone_path,
                state: State::Pending,
                stopped_since: None,
                outcome: None,
                reason: None,
                error: None,
                fix: None,
                default_branch: None,
                current_branch: None,
                ahead: 0,
                behind: 0,
                last_pass_at: None,
                last_success_at: None,
                answered: None,
                step: None,
                step_pid: None,
                step_started_at: None,
                unsuccessful_for_s: 0,
                quiet_for_s: 0,
                conflict: None,
                last_repair: None,
                watch: native(),
            },
            autosync,
            unsuccessful_since: None,
            last_progress: now,
            carried: false,
            in_flight: false,
            failures: 0,
            last_pass_end: None,
        }
    }

    /// Continues the previous daemon's entry: same state, timers that far along.
    pub fn restore(prev: CloneEntry, autosync: bool, now: Instant) -> Self {
        let ago = |s: u64| now.checked_sub(Duration::from_secs(s)).unwrap_or(now);
        let unsuccessful = prev.outcome.as_deref().is_some_and(|o| o != "synced");
        let mut t = Tracker::new(prev.clone_path.clone(), autosync, now);
        t.unsuccessful_since = unsuccessful.then(|| ago(prev.unsuccessful_for_s));
        t.last_progress = ago(prev.quiet_for_s);
        t.carried = prev.step.is_some();
        t.failures = u32::from(unsuccessful);
        t.entry = prev;
        t
    }

    pub fn entry(&self) -> &CloneEntry {
        &self.entry
    }

    pub fn set_watch(&mut self, mode: &str) {
        self.entry.watch = mode.to_string();
    }

    pub fn set_autosync(&mut self, autosync: bool, now: Instant) {
        if autosync && !self.autosync {
            self.last_progress = now;
        }
        self.autosync = autosync;
    }

    /// The main thread asked the worker for a pass.
    pub fn requested(&mut self) {
        self.in_flight = true;
    }

    /// The worker found its thread dead: nothing is in flight any more.
    pub fn worker_died(&mut self) {
        self.in_flight = false;
    }

    pub fn on_step(&mut self, step: &str, pid: u32, now: Instant, wall: SystemTime) {
        self.in_flight = true;
        self.entry.step = Some(step.to_string());
        self.entry.step_pid = Some(pid);
        self.entry.step_started_at = Some(iso(wall));
        if !self.carried {
            self.last_progress = now;
        }
    }

    /// A pass finished. `result` is `None` for an off clone whose clone exists.
    pub fn on_pass(
        &mut self,
        answered: String,
        result: Option<PassResult>,
        now: Instant,
        wall: SystemTime,
    ) {
        let e = &mut self.entry;
        e.step = None;
        e.step_pid = None;
        e.step_started_at = None;
        e.answered = Some(answered);
        e.last_pass_at = Some(iso(wall));
        self.last_progress = now;
        self.carried = false;
        self.in_flight = false;
        self.last_pass_end = Some((now, wall));
        let Some(r) = result else {
            self.unsuccessful_since = None;
            self.failures = 0;
            e.outcome = None;
            e.reason = None;
            e.error = None;
            e.fix = None;
            e.conflict = None;
            return;
        };
        e.outcome = Some(r.outcome.as_str().to_string());
        e.reason = r.reason.map(|r| r.as_str().to_string());
        e.error = r.error;
        e.fix = r.fix;
        e.default_branch = r.default_branch;
        e.current_branch = r.current_branch;
        e.ahead = r.ahead;
        e.behind = r.behind;
        e.conflict = r.conflict.map(|c| ConflictEntry {
            since: e
                .conflict
                .as_ref()
                .map_or_else(|| iso(wall), |old| old.since.clone()),
            paths: c.paths,
            messages: c.messages,
            local_sha: c.local_sha,
            remote_sha: c.remote_sha,
        });
        if let Some(rep) = r.repair {
            e.last_repair = Some(RepairEntry {
                from: rep.from,
                commits: rep.commits,
                at: rep.at,
            });
        }
        if r.outcome == Outcome::Synced {
            e.last_success_at = Some(iso(wall));
            e.stopped_since = None;
            self.unsuccessful_since = None;
            self.failures = 0;
        } else {
            self.unsuccessful_since.get_or_insert(now);
            self.failures += 1;
        }
    }

    /// Decides the state from the timers and fills the written durations. Returns why
    /// the clone just became stopped, once per stop.
    pub fn judge(&mut self, now: Instant, wall: SystemTime) -> Option<BecameStopped> {
        let e = &mut self.entry;
        let off = !self.autosync && e.outcome.is_none() && e.last_pass_at.is_some();
        if off {
            // Nothing is expected of an off clone: its timers don't run.
            self.last_progress = now;
            e.stopped_since = None;
            e.state = State::Off;
            e.unsuccessful_for_s = 0;
            e.quiet_for_s = 0;
            return None;
        }
        let quiet = now.saturating_duration_since(self.last_progress);
        let unsuccessful = self
            .unsuccessful_since
            .map_or(Duration::ZERO, |s| now.saturating_duration_since(s));
        e.quiet_for_s = quiet.as_secs();
        e.unsuccessful_for_s = unsuccessful.as_secs();

        let last_stops = match (e.outcome.as_deref(), e.reason.as_deref()) {
            (Some("conflicted"), _) => Some("conflicted".to_string()),
            (Some("blocked"), Some(r @ ("off_branch" | "not_a_repository"))) => Some(r.to_string()),
            _ => None,
        };
        let timed_out =
            (unsuccessful >= STOP_AFTER).then(|| "passes unsuccessful for 15 min".to_string());
        let silent = (quiet >= STOP_AFTER).then(|| "no progress for 15 min".to_string());

        let mut became = None;
        if e.stopped_since.is_none() {
            if let Some(why) = last_stops.or(timed_out).or(silent.clone()) {
                e.stopped_since = Some(iso(wall));
                became = Some(BecameStopped(why));
            }
        }
        if silent.is_some() {
            e.fix = Some(match (&e.step, e.step_pid, &e.step_started_at) {
                (Some(step), Some(pid), Some(at)) => {
                    format!("git {step} (pid {pid}) has run since {at} with no result.")
                }
                _ => "The worker has reported nothing for 15 min.".to_string(),
            });
        }
        e.state = if e.stopped_since.is_some() {
            State::Stopped
        } else {
            match e.outcome.as_deref() {
                None => State::Pending,
                Some("synced") => State::Synced,
                Some(_) => State::Retrying,
            }
        };
        became
    }

    /// Whether the main thread should ask the worker for a pass now, and why.
    pub fn due(&self, now: Instant, wall: SystemTime) -> Option<&'static str> {
        // An off clone is only retried when making sure it exists failed.
        if self.in_flight || (!self.autosync && self.entry.outcome.is_none()) {
            return None;
        }
        let (ended, ended_wall) = self.last_pass_end?;
        let since = now.saturating_duration_since(ended);
        if self.entry.outcome.as_deref() != Some("synced") && (1..=RETRIES).contains(&self.failures)
        {
            // A wall clock that stepped backward has no say in a retry either.
            if since >= RETRY_STEP * self.failures {
                return Some("retry");
            }
            return None;
        }
        // Instant doesn't count a suspend and the wall clock can step backward: either
        // one reaching the tick is enough.
        let wall_since = wall.duration_since(ended_wall).unwrap_or_default();
        (since >= TICK || wall_since >= TICK).then_some("tick")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pass::{Conflict, Reason};

    const MIN: Duration = Duration::from_secs(60);

    fn result(outcome: Outcome, reason: Option<Reason>) -> PassResult {
        PassResult {
            outcome,
            reason,
            error: reason.map(|r| format!("{} happened", r.as_str())),
            fix: (outcome != Outcome::Synced).then(|| "do the fix".to_string()),
            default_branch: Some("main".into()),
            current_branch: Some("main".into()),
            ahead: 0,
            behind: 0,
            conflict: None,
            repair: None,
        }
    }

    fn failed() -> PassResult {
        result(Outcome::Failed, Some(Reason::FetchFailed))
    }

    struct Clock {
        t0: Instant,
        w0: SystemTime,
        at: Duration,
    }

    impl Clock {
        fn new() -> Self {
            Clock {
                t0: Instant::now(),
                w0: SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000),
                at: Duration::ZERO,
            }
        }
        fn advance(&mut self, d: Duration) -> (Instant, SystemTime) {
            self.at += d;
            self.now()
        }
        fn now(&self) -> (Instant, SystemTime) {
            (self.t0 + self.at, self.w0 + self.at)
        }
    }

    fn tracker(c: &Clock) -> Tracker {
        Tracker::new("/clone".into(), true, c.now().0)
    }

    fn finish(t: &mut Tracker, c: &Clock, r: PassResult) -> Option<BecameStopped> {
        let (i, w) = c.now();
        t.on_pass("w1".into(), Some(r), i, w);
        t.judge(i, w)
    }

    #[test]
    fn a_new_clone_is_pending_then_follows_its_pass() {
        let c = Clock::new();
        let mut t = tracker(&c);
        let (i, w) = c.now();
        t.judge(i, w);
        assert_eq!(t.entry().state, State::Pending);
        finish(&mut t, &c, result(Outcome::Synced, None));
        assert_eq!(t.entry().state, State::Synced);
        finish(&mut t, &c, failed());
        assert_eq!(t.entry().state, State::Retrying);
    }

    #[test]
    fn the_last_pass_alone_stops_a_clone() {
        for r in [
            result(Outcome::Conflicted, None),
            result(Outcome::Blocked, Some(Reason::OffBranch)),
            result(Outcome::Blocked, Some(Reason::NotARepository)),
        ] {
            let c = Clock::new();
            let mut t = tracker(&c);
            assert!(finish(&mut t, &c, r).is_some());
            assert_eq!(t.entry().state, State::Stopped);
        }
        let c = Clock::new();
        let mut t = tracker(&c);
        assert!(finish(
            &mut t,
            &c,
            result(Outcome::Blocked, Some(Reason::ForeignOperation))
        )
        .is_none());
        assert_eq!(t.entry().state, State::Retrying);
    }

    #[test]
    fn unsuccessful_passes_stop_a_clone_after_fifteen_minutes() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, failed());
        for _ in 0..14 {
            c.advance(MIN);
            let (i, w) = c.now();
            t.on_pass("w".into(), Some(failed()), i, w);
            assert!(t.judge(i, w).is_none(), "stopped early at {:?}", c.at);
        }
        assert_eq!(t.entry().state, State::Retrying);
        let (i, w) = c.advance(MIN);
        t.on_pass("w".into(), Some(failed()), i, w);
        assert!(t.judge(i, w).is_some());
        assert_eq!(t.entry().state, State::Stopped);
        // Counted from the first failure of the run, not the latest.
        assert_eq!(t.entry().unsuccessful_for_s, 15 * 60);
    }

    #[test]
    fn a_success_breaks_the_run() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, failed());
        c.advance(14 * MIN);
        finish(&mut t, &c, result(Outcome::Synced, None));
        c.advance(14 * MIN);
        assert!(finish(&mut t, &c, failed()).is_none());
        assert_eq!(t.entry().state, State::Retrying);
    }

    #[test]
    fn silence_stops_a_clone_and_names_the_step() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, result(Outcome::Synced, None));
        let (i, w) = c.advance(MIN);
        t.on_step("fetch", 4242, i, w);
        let (i, w) = c.advance(14 * MIN);
        assert!(t.judge(i, w).is_none());
        let (i, w) = c.advance(MIN);
        assert!(t.judge(i, w).is_some());
        let e = t.entry();
        assert_eq!(e.state, State::Stopped);
        assert!(e.fix.as_deref().unwrap().contains("git fetch (pid 4242)"));
        // Still stopped, and not announced again, until a pass ends synced.
        let (i, w) = c.advance(MIN);
        assert!(t.judge(i, w).is_none());
        assert_eq!(t.entry().state, State::Stopped);
        t.on_pass("w".into(), Some(result(Outcome::Synced, None)), i, w);
        t.judge(i, w);
        assert_eq!(t.entry().state, State::Synced);
        assert_eq!(t.entry().stopped_since, None);
    }

    #[test]
    fn a_step_report_is_progress() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, result(Outcome::Synced, None));
        let (i, w) = c.advance(14 * MIN);
        t.on_step("add", 1, i, w);
        let (i, w) = c.advance(14 * MIN);
        assert!(t.judge(i, w).is_none());
    }

    #[test]
    fn a_restart_continues_the_timers_and_keeps_a_stopped_clone_stopped() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, failed());
        for _ in 0..10 {
            c.advance(MIN);
            finish(&mut t, &c, failed());
        }
        let saved = t.entry().clone();
        assert_eq!(saved.unsuccessful_for_s, 10 * 60);

        // Restarted after a crash: the timer is 10 minutes along, not zero. Passes keep
        // finishing, so only the unsuccessful run can stop it.
        let mut c2 = Clock::new();
        let mut t2 = Tracker::restore(saved, true, c2.now().0);
        let (i, w) = c2.advance(4 * MIN);
        t2.on_pass("w".into(), Some(failed()), i, w);
        assert!(t2.judge(i, w).is_none());
        assert_eq!(t2.entry().state, State::Retrying);
        let (i, w) = c2.advance(MIN);
        t2.on_pass("w".into(), Some(failed()), i, w);
        assert!(t2.judge(i, w).is_some());
        let stopped = t2.entry().clone();
        assert_eq!(stopped.state, State::Stopped);

        // A third incarnation restores the stopped clone as stopped, with no new stop.
        let c3 = Clock::new();
        let mut t3 = Tracker::restore(stopped, true, c3.now().0);
        let (i, w) = c3.now();
        assert!(t3.judge(i, w).is_none());
        assert_eq!(t3.entry().state, State::Stopped);
    }

    #[test]
    fn a_daemon_restarted_every_five_minutes_still_stops_a_clone_that_never_finishes() {
        let mut saved: Option<CloneEntry> = None;
        let mut stopped_at = None;
        for restart in 0..5 {
            let mut c = Clock::new();
            let mut t = match saved.take() {
                Some(prev) => Tracker::restore(prev, true, c.now().0),
                None => tracker(&c),
            };
            // Each incarnation starts the pass again; its git steps start, then hang.
            let (i, w) = c.advance(Duration::from_secs(1));
            t.on_step("fetch", 100 + restart, i, w);
            let (i, w) = c.advance(5 * MIN);
            if t.judge(i, w).is_some() {
                stopped_at = Some(restart);
            }
            saved = Some(t.entry().clone());
        }
        // 3 incarnations of 5 minutes: the stop lands on the third.
        assert_eq!(stopped_at, Some(2));
    }

    #[test]
    fn a_healthy_restart_mid_pass_is_not_stopped() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        let (i, w) = c.advance(MIN);
        t.on_step("fetch", 9, i, w);
        let saved = t.entry().clone();
        let mut c2 = Clock::new();
        let mut t2 = Tracker::restore(saved, true, c2.now().0);
        let (i, w) = c2.advance(MIN);
        t2.on_step("fetch", 10, i, w);
        t2.on_pass("w".into(), Some(result(Outcome::Synced, None)), i, w);
        let (i, w) = c2.advance(14 * MIN);
        assert!(t2.judge(i, w).is_none());
        assert_eq!(t2.entry().state, State::Synced);
    }

    #[test]
    fn retries_come_at_one_then_two_minutes_then_the_tick() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, failed());
        let (i, w) = c.advance(MIN - Duration::from_secs(1));
        assert_eq!(t.due(i, w), None);
        let (i, w) = c.advance(Duration::from_secs(1));
        assert_eq!(t.due(i, w), Some("retry"));
        t.requested();
        finish(&mut t, &c, failed());
        let (i, w) = c.advance(MIN);
        assert_eq!(t.due(i, w), None);
        let (i, w) = c.advance(MIN);
        assert_eq!(t.due(i, w), Some("retry"));
        finish(&mut t, &c, failed());
        let (i, w) = c.advance(4 * MIN);
        assert_eq!(t.due(i, w), None);
        let (i, w) = c.advance(MIN);
        assert_eq!(t.due(i, w), Some("tick"));
    }

    #[test]
    fn the_tick_fires_on_either_clock() {
        // Wall clock jumped forward (a suspend, which Instant doesn't count).
        let c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, result(Outcome::Synced, None));
        let (i, w) = c.now();
        assert_eq!(t.due(i + MIN, w + 5 * MIN), Some("tick"));
        // Wall clock stepped backward: Instant alone still ticks.
        assert_eq!(
            t.due(i + 5 * MIN, w - 3600 * Duration::from_secs(1)),
            Some("tick")
        );
        assert_eq!(t.due(i + 4 * MIN, w - Duration::from_secs(3600)), None);
        // Nothing while a pass is in flight.
        t.requested();
        assert_eq!(t.due(i + 9 * MIN, w + 9 * MIN), None);
    }

    #[test]
    fn an_off_clone_has_no_timers() {
        let mut c = Clock::new();
        let mut t = Tracker::new("/clone".into(), false, c.now().0);
        let (i, w) = c.now();
        t.on_pass("w".into(), None, i, w);
        let (i, w) = c.advance(60 * MIN);
        assert!(t.judge(i, w).is_none());
        assert_eq!(t.entry().state, State::Off);
        assert_eq!(t.due(i, w), None);
    }

    #[test]
    fn a_conflict_keeps_its_first_seen_time() {
        let mut c = Clock::new();
        let mut t = tracker(&c);
        let mut r = result(Outcome::Conflicted, None);
        r.conflict = Some(Conflict {
            paths: vec!["a.md".into()],
            messages: "m".into(),
            local_sha: "l".into(),
            remote_sha: "r".into(),
        });
        finish(&mut t, &c, r.clone());
        let first = t.entry().conflict.clone().unwrap().since;
        c.advance(MIN);
        finish(&mut t, &c, r);
        assert_eq!(t.entry().conflict.as_ref().unwrap().since, first);
    }

    #[test]
    fn instant_does_not_count_a_suspend() {
        // Pinned because std doesn't promise it (CLOCK_MONOTONIC on Linux,
        // CLOCK_UPTIME_RAW on macOS). Run by hand across a real suspend; here it checks
        // that Instant tracks the monotonic clock rather than the wall clock.
        let wall = SystemTime::now();
        let t = Instant::now();
        std::thread::sleep(Duration::from_millis(20));
        let (inst, real) = (t.elapsed(), wall.elapsed().unwrap());
        assert!(inst >= Duration::from_millis(20) && inst <= real + Duration::from_millis(5));
    }

    #[test]
    fn the_file_round_trips_and_a_torn_one_is_ignored() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("state.json");
        let c = Clock::new();
        let mut t = tracker(&c);
        finish(&mut t, &c, failed());
        let file = StateFile {
            v: FORMAT_V,
            pid: 1,
            version: "0.1.0".into(),
            started_at: iso(c.w0),
            alive_at: iso(c.w0),
            scanned_at: iso(c.w0),
            skipped_records: vec![],
            clones: BTreeMap::from([("k".to_string(), t.entry().clone())]),
        };
        write(&path, &file).unwrap();
        assert_eq!(read_previous(&path), file.clones);
        fs::write(&path, "{\"v\":1,").unwrap();
        assert!(read_previous(&path).is_empty());
        fs::write(
            &path,
            serde_json::to_string(&StateFile { v: 2, ..file }).unwrap(),
        )
        .unwrap();
        assert!(read_previous(&path).is_empty());
    }
}
