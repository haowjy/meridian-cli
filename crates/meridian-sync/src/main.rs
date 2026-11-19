use std::path::PathBuf;
use std::process::ExitCode;

use meridian_sync::{daemon, version};

const USAGE: &str =
    "usage: meridian-sync version\n       meridian-sync run --home <absolute home> [--no-wait]";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["version"] => {
            println!("{}", version());
            ExitCode::SUCCESS
        }
        ["run", rest @ ..] => match parse_run(rest) {
            Some(options) => ExitCode::from(daemon::run(options)),
            None => usage(),
        },
        _ => usage(),
    }
}

fn parse_run(args: &[&str]) -> Option<daemon::Options> {
    let (mut home, mut wait) = (None, true);
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match *arg {
            "--home" => home = Some(PathBuf::from(it.next()?)),
            "--no-wait" => wait = false,
            _ => return None,
        }
    }
    let home = home.filter(|h| h.is_absolute())?;
    Some(daemon::Options { home, wait })
}

fn usage() -> ExitCode {
    eprintln!("{USAGE}");
    ExitCode::from(2)
}
