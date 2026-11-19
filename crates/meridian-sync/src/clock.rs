//! Wall-clock timestamps in the one format every file and commit message uses.

use std::time::{SystemTime, UNIX_EPOCH};

/// `2026-11-19T14:00:00.000000Z`: UTC, six fractional digits, so two values compare as
/// strings. Python writes the same shape (`%Y-%m-%dT%H:%M:%S.%fZ`).
pub fn iso(t: SystemTime) -> String {
    let d = t.duration_since(UNIX_EPOCH).unwrap_or_default();
    format_iso(d.as_secs() as i64, d.subsec_micros())
}

pub fn iso_now() -> String {
    iso(SystemTime::now())
}

fn format_iso(secs: i64, micros: u32) -> String {
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
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{micros:06}Z",
        rem / 3_600,
        rem % 3_600 / 60,
        rem % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_known_instants() {
        assert_eq!(format_iso(0, 0), "1970-01-01T00:00:00.000000Z");
        assert_eq!(
            format_iso(951_782_400 + 86_399, 5),
            "2000-02-29T23:59:59.000005Z"
        );
        assert_eq!(
            format_iso(1_795_096_800, 123_456),
            "2026-11-19T14:00:00.123456Z"
        );
    }
}
