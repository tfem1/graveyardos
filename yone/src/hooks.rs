//! Post-transaction hooks: declarative, shipped by the packages that need them.
//!
//! A hook is a `key = value` file in `ROOT/usr/share/yone/hooks/` (from a
//! package) or `ROOT/etc/yone/hooks/` (from the administrator; a file with
//! the same name replaces the packaged one, an empty file disables it):
//!
//! ```text
//! desc  = Compiling GSettings schemas
//! paths = usr/share/glib-2.0/schemas/
//! on    = install upgrade remove        # default: all three
//! run   = /usr/bin/glib-compile-schemas /usr/share/glib-2.0/schemas
//! ```
//!
//! After a committed transaction, each hook whose `paths` prefixes match a
//! changed file runs once, in file-name order, inside the root (directly for
//! `/`, through chroot otherwise). A hook whose program is missing from the
//! root is skipped (bootstrap). A failing hook is reported but never undoes
//! the transaction, which is already complete.
//!
//! Hooks in pacman's format (`usr/share/libalpm/hooks/*.hook`, shipped by
//! the packages converted from Arch's recipes) run too: path triggers with
//! their glob targets (`!` excludes), Install/Upgrade/Remove, post-transaction
//! actions, NeedsTargets (the matching paths on standard input). Package
//! triggers and pre-transaction actions are not run.

use crate::config::Config;
use crate::pkginfo::parse_kv;
use crate::ui::Ui;
use crate::util;
use std::collections::BTreeMap;
use std::fs;
use std::process::Command;

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Op {
    Install,
    Upgrade,
    Remove,
}

#[derive(Debug)]
pub struct Hook {
    pub name: String,
    pub desc: String,
    pub paths: Vec<String>,
    pub on: Vec<Op>,
    pub run: String,
    /// pacman's format: ordered glob targets (excluded when true), instead
    /// of `paths` prefixes
    pub globs: Vec<(bool, String)>,
    /// pacman's NeedsTargets: the matching paths go to standard input
    pub needs_targets: bool,
}

impl Hook {
    fn matches(&self, path: &str) -> bool {
        if self.globs.is_empty() {
            return self.paths.iter().any(|p| path.starts_with(p.as_str()));
        }
        let mut hit = false;
        for (neg, g) in &self.globs {
            if glob(g.as_bytes(), path.as_bytes()) {
                hit = !neg;
            }
        }
        hit
    }
}

/// fnmatch without flags, as pacman uses it: `*` and `?` match `/` too;
/// `[...]` sets with ranges and `!`/`^` negation.
fn glob(p: &[u8], s: &[u8]) -> bool {
    match p.first() {
        None => s.is_empty(),
        Some(b'*') => (0..=s.len()).any(|i| glob(&p[1..], &s[i..])),
        Some(b'?') => !s.is_empty() && glob(&p[1..], &s[1..]),
        Some(b'[') => {
            let Some(&c) = s.first() else { return false };
            let mut i = 1;
            let neg = matches!(p.get(1), Some(b'!') | Some(b'^'));
            if neg {
                i += 1;
            }
            let start = i;
            let mut found = false;
            while i < p.len() && (p[i] != b']' || i == start) {
                if i + 2 < p.len() && p[i + 1] == b'-' && p[i + 2] != b']' {
                    found |= p[i] <= c && c <= p[i + 2];
                    i += 3;
                } else {
                    found |= p[i] == c;
                    i += 1;
                }
            }
            if i >= p.len() {
                // no closing bracket: a literal '['
                return c == b'[' && glob(&p[1..], &s[1..]);
            }
            found != neg && glob(&p[i + 1..], &s[1..])
        }
        Some(&c) => s.first() == Some(&c) && glob(&p[1..], &s[1..]),
    }
}

/// A hook in pacman's format (alpm-hooks(5)).
fn parse_alpm(name: &str, text: &str) -> Result<Option<Hook>, String> {
    let mut section = String::new();
    let (mut globs, mut on, mut path_trigger, mut other_trigger) = (vec![], vec![], false, false);
    let (mut desc, mut run, mut when, mut needs_targets) =
        (String::new(), String::new(), String::new(), false);
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            section = line[1..line.len() - 1].to_string();
            continue;
        }
        let (k, v) = match line.split_once('=') {
            Some((k, v)) => (k.trim(), v.trim()),
            None => (line, ""),
        };
        match (section.as_str(), k) {
            ("Trigger", "Type") => match v {
                "Path" | "File" => path_trigger = true,
                _ => other_trigger = true,
            },
            ("Trigger", "Operation") => on.push(match v {
                "Install" => Op::Install,
                "Upgrade" => Op::Upgrade,
                "Remove" => Op::Remove,
                other => return Err(format!("unknown Operation '{other}'")),
            }),
            ("Trigger", "Target") => match v.strip_prefix('!') {
                Some(g) => globs.push((true, g.trim_start_matches('/').to_string())),
                None => globs.push((false, v.trim_start_matches('/').to_string())),
            },
            ("Action", "Description") => desc = v.to_string(),
            ("Action", "Exec") => run = v.to_string(),
            ("Action", "When") => when = v.to_string(),
            ("Action", "NeedsTargets") => needs_targets = true,
            _ => {}
        }
    }
    // package triggers and pre-transaction actions: not run by Yone
    if !path_trigger || other_trigger || when != "PostTransaction" || run.is_empty() {
        return Ok(None);
    }
    if globs.is_empty() {
        return Err("a Path trigger without Target".into());
    }
    on.sort_by_key(|o| *o as u8);
    on.dedup();
    Ok(Some(Hook {
        name: name.to_string(),
        desc: if desc.is_empty() {
            name.trim_end_matches(".hook").to_string()
        } else {
            desc
        },
        paths: vec![],
        on,
        run,
        globs,
        needs_targets,
    }))
}

/// Built in, so the base system works before any package ships hooks.
const BUILTIN: &[(&str, &str)] = &[(
    "10-ldconfig.hook",
    "desc = Updating the shared library cache\n\
     paths = usr/lib/ usr/lib64/\n\
     run = /usr/sbin/ldconfig\n",
)];

fn parse(name: &str, text: &str) -> Result<Option<Hook>, String> {
    if text.lines().any(|l| l.trim() == "[Trigger]") {
        return parse_alpm(name, text);
    }
    let kv = parse_kv(text).map_err(|e| e.to_string())?;
    if kv.is_empty() {
        return Ok(None); // empty file: disabled
    }
    let get = |k: &str| {
        kv.iter()
            .filter(|(x, _)| x == k)
            .map(|(_, v)| v.as_str())
            .collect::<Vec<_>>()
            .join(" ")
    };
    let run = get("run");
    let paths: Vec<String> = get("paths")
        .split_whitespace()
        .map(|p| p.trim_start_matches('/').to_string())
        .collect();
    if run.is_empty() || paths.is_empty() {
        return Err("needs `paths` and `run`".into());
    }
    let mut on = vec![];
    for w in get("on").split_whitespace() {
        on.push(match w {
            "install" => Op::Install,
            "upgrade" => Op::Upgrade,
            "remove" => Op::Remove,
            other => return Err(format!("unknown `on` value '{other}'")),
        });
    }
    if on.is_empty() {
        on = vec![Op::Install, Op::Upgrade, Op::Remove];
    }
    let desc = match get("desc") {
        d if d.is_empty() => name.trim_end_matches(".hook").to_string(),
        d => d,
    };
    Ok(Some(Hook {
        name: name.to_string(),
        desc,
        paths,
        on,
        run,
        globs: vec![],
        needs_targets: false,
    }))
}

/// Every active hook, the administrator's overriding the packaged ones.
pub fn load(cfg: &Config, ui: &Ui) -> Vec<Hook> {
    let mut texts: BTreeMap<String, String> = BTreeMap::new();
    for (n, t) in BUILTIN {
        texts.insert(n.to_string(), t.to_string());
    }
    // pacman's first: a Yone hook of the same name replaces it
    for dir in [
        "usr/share/libalpm/hooks",
        "usr/share/yone/hooks",
        "etc/yone/hooks",
    ] {
        let Ok(rd) = fs::read_dir(cfg.root.join(dir)) else {
            continue;
        };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if name.ends_with(".hook") {
                if let Ok(t) = fs::read_to_string(e.path()) {
                    texts.insert(name, t);
                }
            }
        }
    }
    let mut hooks = vec![];
    for (name, text) in texts {
        match parse(&name, &text) {
            Ok(Some(h)) => hooks.push(h),
            Ok(None) => {}
            Err(e) => ui.warn(&format!("hook {name} ignored: {e}")),
        }
    }
    hooks
}

/// Run the hooks matching `changed` (root-relative paths) for `ops`.
pub fn run(cfg: &Config, ui: &Ui, ops: &[Op], changed: &[String]) {
    for h in load(cfg, ui) {
        if !h.on.iter().any(|o| ops.contains(o)) {
            continue;
        }
        let targets: Vec<&String> = changed.iter().filter(|c| h.matches(c)).collect();
        if targets.is_empty() {
            continue;
        }
        let prog = h.run.split_whitespace().next().unwrap_or("");
        if prog.starts_with('/') && fs::metadata(cfg.root.join(&prog[1..])).is_err() {
            continue; // the program is not installed in this root yet
        }
        let mut cmd = if cfg.is_live_root() {
            let mut c = Command::new("/bin/sh");
            c.arg("-c").arg(&h.run);
            c
        } else {
            if util::euid() != Some(0) {
                ui.warn(&format!(
                    "hook '{}' skipped: running it in {} needs root",
                    h.desc,
                    cfg.root.display()
                ));
                continue;
            }
            let mut c = Command::new("chroot");
            c.arg(&cfg.root).arg("/bin/sh").arg("-c").arg(&h.run);
            c
        };
        let status = if h.needs_targets {
            use std::io::Write;
            cmd.stdin(std::process::Stdio::piped());
            cmd.spawn().and_then(|mut child| {
                if let Some(mut input) = child.stdin.take() {
                    let mut list = String::new();
                    for t in &targets {
                        list.push_str(t);
                        list.push('\n');
                    }
                    let _ = input.write_all(list.as_bytes());
                }
                child.wait()
            })
        } else {
            cmd.status()
        };
        match status {
            Ok(s) if s.success() => ui.info(&format!("hook: {}", h.desc)),
            Ok(s) => ui.warn(&format!("hook '{}' failed ({s})", h.desc)),
            Err(e) => ui.warn(&format!("hook '{}' could not run: {e}", h.desc)),
        }
        util::log(cfg, &format!("hook {} ({})", h.name, h.desc));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hooks() {
        let h = parse(
            "20-schemas.hook",
            "desc = Schemas\npaths = /usr/share/glib-2.0/schemas/\non = install upgrade\nrun = /usr/bin/x a b\n",
        )
        .unwrap()
        .unwrap();
        assert_eq!(h.paths, vec!["usr/share/glib-2.0/schemas/"]);
        assert_eq!(h.on, vec![Op::Install, Op::Upgrade]);
        assert_eq!(h.run, "/usr/bin/x a b");
        assert!(
            parse("x.hook", "").unwrap().is_none(),
            "an empty file disables a hook"
        );
        assert!(
            parse("x.hook", "run = true\n").is_err(),
            "paths are required"
        );
        assert!(parse("x.hook", "paths = a/\nrun = true\non = reboot\n").is_err());
        let d = parse("30-x.hook", "paths = a/\nrun = true\n")
            .unwrap()
            .unwrap();
        assert_eq!((d.desc.as_str(), d.on.len()), ("30-x", 3));
    }

    #[test]
    fn pacman_hooks() {
        let schemas = "[Trigger]\nType = Path\nOperation = Install\nOperation = Upgrade\nOperation = Remove\n\
                       Target = usr/share/glib-2.0/schemas/*.gschema.xml\nTarget = usr/share/glib-2.0/schemas/*.gschema.override\n\n\
                       [Action]\nDescription = Compiling GSettings XML schema files...\nWhen = PostTransaction\n\
                       Exec = /usr/bin/glib-compile-schemas /usr/share/glib-2.0/schemas\n";
        let h = parse("glib-compile-schemas.hook", schemas)
            .unwrap()
            .unwrap();
        assert!(h.matches("usr/share/glib-2.0/schemas/org.gnome.foo.gschema.xml"));
        assert!(!h.matches("usr/share/glib-2.0/schemas/gschemas.compiled"));
        assert_eq!(h.on, vec![Op::Install, Op::Upgrade, Op::Remove]);
        assert!(!h.needs_targets);

        let icons = "[Trigger]\nType = Path\nOperation = Install\nOperation = Upgrade\nOperation = Remove\n\
                     Target = usr/share/icons/*/\nTarget = !usr/share/icons/*/?*\n\n\
                     [Action]\nDescription = Updating icon theme caches...\nWhen = PostTransaction\n\
                     Exec = /usr/share/libalpm/scripts/gtk-update-icon-cache\nNeedsTargets\n";
        let h = parse("gtk-update-icon-cache.hook", icons).unwrap().unwrap();
        assert!(h.matches("usr/share/icons/hicolor/"));
        assert!(!h.matches("usr/share/icons/hicolor/index.theme"));
        assert!(h.needs_targets);

        let pre = "[Trigger]\nType = Path\nOperation = Remove\nTarget = usr/lib/x\n[Action]\nWhen = PreTransaction\nExec = /bin/true\n";
        assert!(parse("pre.hook", pre).unwrap().is_none());
        let pkg = "[Trigger]\nType = Package\nOperation = Install\nTarget = linux\n[Action]\nWhen = PostTransaction\nExec = /bin/true\n";
        assert!(parse("pkg.hook", pkg).unwrap().is_none());
    }

    #[test]
    fn globs() {
        assert!(glob(b"usr/lib/*.so", b"usr/lib/a/b.so"));
        assert!(glob(b"usr/share/[a-c]x", b"usr/share/bx"));
        assert!(!glob(b"usr/share/[!a-c]x", b"usr/share/bx"));
        assert!(glob(b"a?c", b"abc"));
        assert!(!glob(b"a?c", b"ac"));
    }
}
