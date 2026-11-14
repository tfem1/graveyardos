//! Terminal output. Progress rendering is throttled so it never costs
//! measurable time; without a TTY Yone prints plain, log-friendly lines.

use crate::error::Result;
use std::io::{self, BufRead, IsTerminal, Write};
use std::time::{Duration, Instant};

pub struct Ui {
    pub color: bool,
    pub tty: bool,
    pub noconfirm: bool,
}

impl Ui {
    pub fn new(color_mode: &str, noconfirm: bool) -> Ui {
        let tty = io::stderr().is_terminal();
        let color = match color_mode {
            "always" => true,
            "never" => false,
            _ => {
                tty && std::env::var_os("NO_COLOR").is_none()
                    && std::env::var("TERM").map(|t| t != "dumb").unwrap_or(true)
            }
        };
        Ui {
            color,
            tty,
            noconfirm,
        }
    }

    fn paint(&self, code: &str, s: &str) -> String {
        if self.color {
            format!("\x1b[{code}m{s}\x1b[0m")
        } else {
            s.to_string()
        }
    }
    pub fn accent(&self, s: &str) -> String {
        self.paint("1;97", s) // GraveyardOS: white on black
    }
    pub fn bold(&self, s: &str) -> String {
        self.paint("1", s)
    }
    pub fn dim(&self, s: &str) -> String {
        self.paint("2", s)
    }
    pub fn green(&self, s: &str) -> String {
        self.paint("32", s)
    }
    pub fn red(&self, s: &str) -> String {
        self.paint("1;31", s)
    }
    pub fn yellow(&self, s: &str) -> String {
        self.paint("1;33", s)
    }

    pub fn header(&self, msg: &str) {
        eprintln!("{} {}", self.accent("::"), self.bold(msg));
    }
    pub fn stage(&self, n: usize, total: usize, msg: &str) {
        eprintln!("{} {}", self.dim(&format!("({n}/{total})")), msg);
    }
    pub fn info(&self, msg: &str) {
        eprintln!("   {msg}");
    }
    pub fn warn(&self, msg: &str) {
        eprintln!("{} {}", self.yellow("warning:"), msg);
    }
    pub fn success(&self, msg: &str) {
        eprintln!("{} {}", self.green("✓"), msg);
    }

    pub fn confirm(&self, q: &str) -> Result<bool> {
        if self.noconfirm {
            return Ok(true);
        }
        if !io::stdin().is_terminal() {
            crate::bail!("cannot ask for confirmation without a terminal; re-run with --noconfirm");
        }
        eprint!("{} {} [Y/n] ", self.accent("::"), self.bold(q));
        io::stderr().flush().ok();
        let mut line = String::new();
        io::stdin().lock().read_line(&mut line)?;
        let a = line.trim().to_lowercase();
        Ok(a.is_empty() || a == "y" || a == "yes" || a == "o" || a == "oui")
    }

    /// A smooth bar: white blocks to an eighth of a character, on grey.
    pub fn bar(&self, frac: f64, width: usize) -> String {
        const PARTS: [&str; 8] = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"];
        let eighths = (frac.clamp(0.0, 1.0) * (width * 8) as f64).round() as usize;
        let (full, part) = (eighths / 8, eighths % 8);
        let mut filled = "█".repeat(full);
        let mut used = full;
        if full < width && part > 0 {
            filled.push_str(PARTS[part]);
            used += 1;
        }
        let empty = "░".repeat(width.saturating_sub(used));
        if self.color {
            format!("\x1b[97m{filled}\x1b[0m\x1b[38;5;238m{empty}\x1b[0m")
        } else {
            format!("{filled}{empty}")
        }
    }

    /// The one-line status of a long operation, redrawn in place:
    ///   verb  ████████▌░░░░░░░  12/84  52%  detail
    pub fn status(&self, verb: &str, frac: f64, count: &str, detail: &str) {
        let width = terminal_width();
        let mut line = format!(
            " {} {}  {}  {:>3}%",
            self.bold(&format!("{verb:<11}")),
            self.bar(frac, 28),
            self.dim(count),
            (frac.clamp(0.0, 1.0) * 100.0).round() as u32
        );
        // the detail fills what is left of the line, cut on the left
        let used = 1 + 11 + 1 + 28 + 2 + count.chars().count() + 2 + 4 + 2;
        if width > used + 4 {
            let room = width - used;
            // "name › path": the name stays, the path is cut on its left
            let (keep, cut) = match detail.find(" › ") {
                Some(i) => detail.split_at(i + " › ".len()),
                None => ("", detail),
            };
            let keep_n = keep.chars().count();
            let c: Vec<char> = cut.chars().collect();
            let shown: String = if keep_n + c.len() <= room {
                detail.to_string()
            } else if room > keep_n + 2 {
                let n = room - keep_n - 1;
                format!("{keep}…{}", c[c.len() - n..].iter().collect::<String>())
            } else {
                detail.chars().take(room).collect()
            };
            line.push_str("  ");
            line.push_str(&self.dim(&shown));
        }
        self.progress(&line);
    }

    pub fn progress(&self, line: &str) {
        if self.tty {
            eprint!("\r\x1b[2K{line}");
            io::stderr().flush().ok();
        }
    }
    pub fn progress_end(&self) {
        if self.tty {
            eprint!("\r\x1b[2K");
            io::stderr().flush().ok();
        }
    }
}

/// Columns of the terminal on stderr (80 when it cannot be read).
fn terminal_width() -> usize {
    #[repr(C)]
    struct WinSize {
        row: u16,
        col: u16,
        x: u16,
        y: u16,
    }
    extern "C" {
        fn ioctl(fd: i32, req: u64, ...) -> i32;
    }
    let mut w = WinSize {
        row: 0,
        col: 0,
        x: 0,
        y: 0,
    };
    // TIOCGWINSZ on Linux
    let ok = unsafe { ioctl(2, 0x5413, &mut w as *mut WinSize) } == 0;
    if ok && w.col > 20 {
        w.col as usize
    } else {
        std::env::var("COLUMNS")
            .ok()
            .and_then(|c| c.parse().ok())
            .unwrap_or(80)
    }
}

/// Rate limiter for progress redraws.
pub struct Throttle(Option<Instant>);
impl Throttle {
    pub fn new() -> Self {
        Throttle(None)
    }
    pub fn ready(&mut self) -> bool {
        let now = Instant::now();
        match self.0 {
            Some(t) if now.duration_since(t) < Duration::from_millis(80) => false,
            _ => {
                self.0 = Some(now);
                true
            }
        }
    }
}

pub fn human_bytes(n: u64) -> String {
    let units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = n as f64;
    let mut u = 0;
    while v >= 1024.0 && u < units.len() - 1 {
        v /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", units[u])
    }
}

pub fn human_eta(secs: f64) -> String {
    if !secs.is_finite() || secs < 0.0 {
        return "--:--".into();
    }
    let s = secs.round() as u64;
    if s >= 3600 {
        format!("{}:{:02}:{:02}", s / 3600, s / 60 % 60, s % 60)
    } else {
        format!("{}:{:02}", s / 60, s % 60)
    }
}
