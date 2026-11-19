//! The meridian-sync engine and daemon: git subprocesses, the sync pass, root records,
//! the per-clone workers and the state file.

pub mod clock;
pub mod conflict;
pub mod daemon;
pub mod git;
pub mod pass;
pub mod records;
pub mod state;
pub mod watch;

/// This build's version, spelled the way Python spells it (`0.24.0rc1`, where Cargo
/// writes `0.24.0-rc.1`), so `state.json`, `meridian-sync version` and meridian's own
/// version compare as strings.
pub fn version() -> String {
    env!("CARGO_PKG_VERSION")
        .replace("-alpha.", "a")
        .replace("-beta.", "b")
        .replace("-rc.", "rc")
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_uses_pythons_spelling() {
        assert!(!super::version().contains('-'));
    }
}
