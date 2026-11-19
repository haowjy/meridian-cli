//! The daemon's exit codes, through the real binary: 75 asks for a restart, 0 stops for good.

use std::fs;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

fn start(exe: &Path, home: &Path) -> Child {
    let child = Command::new(exe)
        .args(["run", "--home", home.to_str().unwrap(), "--no-wait"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let lock = home.join("autosync/daemon.lock");
    let deadline = Instant::now() + Duration::from_secs(5);
    while fs::read_to_string(&lock).unwrap_or_default().is_empty() {
        assert!(Instant::now() < deadline, "daemon never took its lock");
        std::thread::sleep(Duration::from_millis(20));
    }
    child
}

fn exit_code(mut child: Child) -> Option<i32> {
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if let Some(status) = child.try_wait().unwrap() {
            return status.code();
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    panic!("daemon did not exit");
}

#[test]
fn the_exit_rules() {
    let tmp = tempfile::tempdir().unwrap();
    let real = Path::new(env!("CARGO_BIN_EXE_meridian-sync"));

    // A lock file replaced under the daemon: a second daemon could hold the lock.
    let home = tmp.path().join("home1");
    fs::create_dir_all(&home).unwrap();
    let child = start(real, &home);
    fs::remove_file(home.join("autosync/daemon.lock")).unwrap();
    assert_eq!(exit_code(child), Some(75));

    // A binary that stays missing past the grace: uninstalled.
    let home = tmp.path().join("home2");
    fs::create_dir_all(&home).unwrap();
    let copy = tmp.path().join("meridian-sync-copy");
    fs::copy(real, &copy).unwrap();
    let child = start(&copy, &home);
    fs::remove_file(&copy).unwrap();
    assert_eq!(exit_code(child), Some(0));

    // A home that is gone at start: nothing is created.
    let gone = tmp.path().join("gone");
    let child = Command::new(real)
        .args(["run", "--home", gone.to_str().unwrap()])
        .spawn()
        .unwrap();
    assert_eq!(exit_code(child), Some(0));
    assert!(!gone.exists());
}
