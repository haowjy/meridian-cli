//! Reading root records (`<home>/autosync/roots/<sha16(root)>.json`), which Python writes,
//! and grouping their contexts into the clones they name.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

/// The only record format this build reads.
const SUPPORTED_V: i64 = 1;

#[derive(Deserialize)]
struct Record {
    root: PathBuf,
    written_at: String,
    #[serde(default)]
    contexts: Vec<ContextEntry>,
}

#[derive(Deserialize)]
struct ContextEntry {
    name: String,
    remote: String,
    clone_path: PathBuf,
    clone_key: String,
    autosync: bool,
    /// The context's directory inside the clone. Not in the design's example record;
    /// `SYNC-CONFLICT.md` needs it.
    #[serde(default)]
    path: Option<String>,
}

/// A context as one record names it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextRef {
    pub root: PathBuf,
    pub name: String,
    pub path: Option<String>,
    pub autosync: bool,
}

/// One clone, with every context in every readable record that names it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloneGroup {
    pub clone_path: PathBuf,
    pub clone_key: String,
    pub remote: String,
    /// True when any naming context has autosync on.
    pub autosync: bool,
    pub contexts: Vec<ContextRef>,
    /// The newest `written_at` among the records naming the clone: a newer value than the
    /// last one seen asks for a pass.
    pub written_at: String,
    /// The root of the most recently written record naming the clone.
    pub newest_root: PathBuf,
}

/// A record file this build refused or couldn't parse.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Skipped {
    pub file: String,
    /// The record's `v` when it had one.
    pub v: Option<i64>,
    pub error: String,
}

#[derive(Debug, Default)]
pub struct Scan {
    pub clones: Vec<CloneGroup>,
    pub skipped: Vec<Skipped>,
}

/// Lists `roots_dir` and groups what it finds. A missing directory is an empty scan.
/// Records whose `root` no longer exists are dropped; they name nothing to sync.
pub fn scan(roots_dir: &Path) -> Scan {
    let mut out = Scan::default();
    let Ok(entries) = fs::read_dir(roots_dir) else {
        return out;
    };
    let mut files: Vec<_> = entries
        .filter_map(Result::ok)
        .filter_map(|e| e.file_name().into_string().ok())
        // A crashed atomic write leaves a `.…tmp` file.
        .filter(|n| n.ends_with(".json") && !n.starts_with('.'))
        .collect();
    files.sort();

    let mut records = Vec::new();
    for file in files {
        match read_record(&roots_dir.join(&file)) {
            Ok(rec) => records.push(rec),
            Err((v, error)) => out.skipped.push(Skipped { file, v, error }),
        }
    }
    records.retain(|r| r.root.is_dir());
    // Oldest first, so the newest record's remote and key win a disagreement.
    records.sort_by(|a, b| a.written_at.cmp(&b.written_at));

    let mut groups: BTreeMap<PathBuf, CloneGroup> = BTreeMap::new();
    for rec in records {
        for ctx in rec.contexts {
            let group = groups
                .entry(ctx.clone_path.clone())
                .or_insert_with(|| CloneGroup {
                    clone_path: ctx.clone_path.clone(),
                    clone_key: String::new(),
                    remote: String::new(),
                    autosync: false,
                    contexts: Vec::new(),
                    written_at: String::new(),
                    newest_root: PathBuf::new(),
                });
            group.clone_key = ctx.clone_key;
            group.remote = ctx.remote;
            group.autosync |= ctx.autosync;
            group.written_at = rec.written_at.clone();
            group.newest_root = rec.root.clone();
            group.contexts.push(ContextRef {
                root: rec.root.clone(),
                name: ctx.name,
                path: ctx.path,
                autosync: ctx.autosync,
            });
        }
    }
    out.clones = groups.into_values().collect();
    out
}

fn read_record(path: &Path) -> Result<Record, (Option<i64>, String)> {
    let text = fs::read_to_string(path).map_err(|e| (None, e.to_string()))?;
    let value: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| (None, e.to_string()))?;
    let v = value.get("v").and_then(serde_json::Value::as_i64);
    if v != Some(SUPPORTED_V) {
        return Err((v, format!("unsupported record version {v:?}")));
    }
    serde_json::from_value(value).map_err(|e| (v, e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, name: &str, body: String) {
        fs::write(dir.join(name), body).unwrap();
    }

    fn record(root: &Path, at: &str, clone: &str, remote: &str, autosync: bool) -> String {
        format!(
            r#"{{"v":1,"root":{root:?},"written_at":"{at}","extra":1,"contexts":[
            {{"name":"kb","remote":"{remote}","clone_path":"{clone}","clone_key":"k1","autosync":{autosync}}}]}}"#
        )
    }

    #[test]
    fn groups_contexts_by_clone() {
        let tmp = tempfile::tempdir().unwrap();
        let (r1, r2, gone) = (
            tmp.path().join("a"),
            tmp.path().join("b"),
            tmp.path().join("x"),
        );
        fs::create_dir_all(&r1).unwrap();
        fs::create_dir_all(&r2).unwrap();
        let roots = tmp.path().join("roots");
        fs::create_dir_all(&roots).unwrap();
        write(
            &roots,
            "1.json",
            record(&r1, "2026-01-01T00:00:00Z", "/c", "old", false),
        );
        write(
            &roots,
            "2.json",
            record(&r2, "2026-01-02T00:00:00Z", "/c", "new", true),
        );
        write(
            &roots,
            "3.json",
            record(&gone, "2026-01-03T00:00:00Z", "/other", "r", true),
        );
        write(&roots, "4.json", r#"{"v":2,"root":"/"}"#.to_string());
        write(&roots, "5.json", "{torn".to_string());
        write(&roots, ".6.json.tmp", "{}".to_string());

        let scan = scan(&roots);
        assert_eq!(scan.clones.len(), 1);
        let c = &scan.clones[0];
        assert_eq!(
            (c.remote.as_str(), c.autosync, c.contexts.len()),
            ("new", true, 2)
        );
        assert_eq!(c.written_at, "2026-01-02T00:00:00Z");
        assert_eq!(c.newest_root, r2);
        let skipped: Vec<_> = scan
            .skipped
            .iter()
            .map(|s| (s.file.as_str(), s.v))
            .collect();
        assert_eq!(skipped, [("4.json", Some(2)), ("5.json", None)]);
    }

    #[test]
    fn missing_directory_is_empty() {
        let scan = scan(Path::new("/nonexistent/roots"));
        assert!(scan.clones.is_empty() && scan.skipped.is_empty());
    }
}
