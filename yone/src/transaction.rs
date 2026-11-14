//! Transactions: dependency resolution, conflict detection, journaled
//! installation with rollback, removal, upgrade, verification.
//!
//! Every filesystem change is journaled in `/var/lib/yone/journal` *before*
//! it happens:
//!   T <path>   temporary `<path>.yone-new` created
//!   B <path>   existing `<path>` moved aside to `<path>.yone-old`
//!   N <path>   `<path>` now holds new content
//!   D <path>   directory created
//!   S <path>   file or symlink left over (upgrade, removal), deleted after commit
//!   R <path>   directory left over, removed after commit if it is a real,
//!              empty directory (never a symlink such as /lib -> usr/lib)
//!   C          commit point: every new file is in place and durable and the
//!              new database entries are staged (`local/.staged`)
//! Before `C`, recovery rolls back (replays the journal backwards and drops
//! the staged database). After `C`, it rolls *forward*: publishes the staged
//! database, deletes the moved-aside originals and the `S` paths. Either way
//! files and database always agree, whatever moment the machine died.

use crate::archive;
use crate::config::{Config, SigLevel};
use crate::db::{FileRec, Installed, LocalDb};
use crate::download::{fetch_all, Job};
use crate::error::{Context, Result};
use crate::pkginfo::{IndexEntry, PkgInfo};
use crate::sha256::{self, HashingWriter};
use crate::sync::SyncDb;
use crate::tar::Kind;
use crate::ui::{human_bytes, Throttle, Ui};
use crate::version::{vercmp, Dep};
use crate::{bail, hooks, sign, util};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

#[derive(Default, Clone)]
pub struct Opts {
    pub overwrite: bool,
    pub allow_unsigned: bool,
    pub reinstall: bool,
    pub asdeps: bool,
    pub cascade: bool,
    pub offline: bool,
}

// ------------------------------------------------------------------ lock

/// Exclusive database lock held with flock(2): the kernel releases it when
/// the process exits, so a crash or `kill -9` never leaves a stale lock.
pub struct Lock(#[allow(dead_code)] File);

extern "C" {
    fn flock(fd: i32, operation: i32) -> i32;
}
const LOCK_EX: i32 = 2;
const LOCK_NB: i32 = 4;

impl Lock {
    pub fn acquire(cfg: &Config) -> Result<Lock> {
        use std::os::unix::io::AsRawFd;
        fs::create_dir_all(&cfg.dbpath).ctx(|| format!("create {}", cfg.dbpath.display()))?;
        let p = cfg.dbpath.join("db.lck");
        let f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(&p)
            .ctx(|| format!("open {}", p.display()))?;
        // SAFETY: flock only reads the descriptor, which `f` keeps open.
        if unsafe { flock(f.as_raw_fd(), LOCK_EX | LOCK_NB) } != 0 {
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() == Some(11) {
                // EWOULDBLOCK
                bail!("database is locked: another yone is running");
            }
            bail!("cannot lock {}: {}", p.display(), e);
        }
        Ok(Lock(f))
    }
}

// ------------------------------------------------------------ interrupts

/// Ctrl-C / SIGTERM during a transaction: the flag is checked between
/// files, and before the commit point the transaction rolls back cleanly
/// instead of leaving a journal behind. After the commit point it finishes.
static INTERRUPTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

extern "C" fn on_signal(_: i32) {
    INTERRUPTED.store(true, std::sync::atomic::Ordering::SeqCst);
}

extern "C" {
    fn signal(signum: i32, handler: usize) -> usize;
    #[cfg(debug_assertions)]
    fn raise(sig: i32) -> i32;
}

fn catch_interrupts() {
    // SAFETY: the handler only stores into an atomic, which is signal-safe.
    unsafe {
        signal(2, on_signal as extern "C" fn(i32) as usize); // SIGINT
        signal(15, on_signal as extern "C" fn(i32) as usize); // SIGTERM
    }
}

fn check_interrupted() -> Result<()> {
    if INTERRUPTED.load(std::sync::atomic::Ordering::SeqCst) {
        bail!("interrupted");
    }
    Ok(())
}

// --------------------------------------------------------------- journal

/// Shared by the threads that extract packages: one record is one line.
struct Journal {
    f: std::sync::Mutex<BufWriter<File>>,
}

impl Journal {
    fn create(cfg: &Config) -> Result<Journal> {
        let path = cfg.dbpath.join("journal");
        if path.exists() {
            bail!("an interrupted transaction was found; run 'yone recover' first");
        }
        LocalDb::open(&cfg.dbpath).discard_staged()?;
        let f = BufWriter::new(File::create(&path)?);
        Ok(Journal {
            f: std::sync::Mutex::new(f),
        })
    }

    fn rec(&self, tag: char, p: &Path) -> Result<()> {
        let mut f = self.f.lock().unwrap_or_else(|e| e.into_inner());
        writeln!(f, "{} {}", tag, p.display())?;
        f.flush()?;
        Ok(())
    }

    fn sync(&mut self) -> Result<()> {
        let f = self.f.get_mut().unwrap_or_else(|e| e.into_inner());
        f.flush()?;
        if util::durable() {
            f.get_ref().sync_all()?;
        }
        Ok(())
    }

    /// Point of no return: records `C` (with `sync = yes`, after flushing
    /// every filesystem write made so far, and durably).
    fn commit_point(&mut self, root: &Path) -> Result<()> {
        if util::durable() {
            sync_fs(root);
        }
        self.f
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .write_all(b"C\n")?;
        self.sync()?;
        crash_point("committed");
        Ok(())
    }
}

extern "C" {
    fn syncfs(fd: i32) -> i32;
}

fn sync_fs(root: &Path) {
    use std::os::unix::io::AsRawFd;
    if let Ok(d) = File::open(root) {
        // SAFETY: syncfs only reads the descriptor, kept open by `d`.
        unsafe { syncfs(d.as_raw_fd()) };
    }
}

/// Test hook (debug builds only): die abruptly at a named point, as a power
/// cut would, to exercise `yone recover`.
fn crash_point(_at: &str) {
    #[cfg(debug_assertions)]
    if std::env::var("YONE_TEST_CRASH").as_deref() == Ok(_at) {
        std::process::exit(137);
    }
}

/// Finish a transaction whose journal holds the commit point: publish the
/// staged database, delete what was moved aside or left over, drop the journal.
fn roll_forward(cfg: &Config) -> Result<usize> {
    let path = cfg.dbpath.join("journal");
    let text = fs::read_to_string(&path)?;
    LocalDb::open(&cfg.dbpath).apply_staged()?;
    let mut n = 0;
    for line in text.lines() {
        let Some((tag, p)) = line.split_once(' ') else {
            continue;
        };
        let p = PathBuf::from(p);
        let (victim, dir_only) = match tag {
            "B" => (util::suffixed(&p, ".yone-old"), false),
            "S" => (p, false),
            "R" => (p, true),
            _ => continue,
        };
        match fs::symlink_metadata(&victim) {
            Ok(m) if m.is_dir() => {
                let _ = fs::remove_dir(&victim);
            }
            Ok(_) if dir_only => {} // a symlink to a directory: not ours to remove
            Ok(_) => util::ignore_missing(fs::remove_file(&victim))?,
            Err(_) => {}
        }
        n += 1;
    }
    fs::remove_file(&path)?;
    Ok(n)
}

pub enum Recovery {
    Clean,
    RolledBack(usize),
    Completed(usize),
}

/// `yone recover`: roll back before the commit point, forward after it.
pub fn recover(cfg: &Config) -> Result<Recovery> {
    let path = cfg.dbpath.join("journal");
    let text = match fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            LocalDb::open(&cfg.dbpath).discard_staged()?;
            return Ok(Recovery::Clean);
        }
        Err(e) => return Err(e.into()),
    };
    if text.lines().any(|l| l == "C") {
        return Ok(Recovery::Completed(roll_forward(cfg)?));
    }
    Ok(Recovery::RolledBack(rollback(cfg)?))
}

/// Undo everything recorded in the journal (only valid before the commit point).
fn rollback(cfg: &Config) -> Result<usize> {
    let path = cfg.dbpath.join("journal");
    let text = match fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e.into()),
    };
    let mut n = 0;
    for line in text.lines().rev() {
        let Some((tag, p)) = line.split_once(' ') else {
            continue;
        };
        let p = PathBuf::from(p);
        match tag {
            "T" => {
                let _ = fs::remove_file(util::suffixed(&p, ".yone-new"));
            }
            "N" => {
                let _ = fs::remove_file(&p);
            }
            "B" => {
                let old = util::suffixed(&p, ".yone-old");
                if fs::symlink_metadata(&old).is_ok() {
                    fs::rename(&old, &p).ctx(|| format!("restore {}", p.display()))?;
                }
            }
            "D" => {
                let _ = fs::remove_dir(&p);
            }
            _ => continue,
        }
        n += 1;
    }
    LocalDb::open(&cfg.dbpath).discard_staged()?;
    fs::remove_file(&path)?;
    Ok(n)
}

// ------------------------------------------------------------ resolution

#[derive(Clone)]
#[allow(clippy::large_enum_variant)]
pub enum Source {
    Repo(IndexEntry),
    File(PathBuf),
}

#[derive(Clone)]
pub struct Item {
    pub info: PkgInfo,
    pub source: Source,
    pub explicit: bool,
}

struct Resolver<'a> {
    sync: &'a SyncDb,
    installed: &'a [Installed],
    pending: HashMap<String, Item>,
    state: HashMap<String, u8>,
    visiting: Vec<PkgInfo>,
    plan: Vec<Item>,
}

impl<'a> Resolver<'a> {
    fn installed_ok(&self, dep: &Dep) -> bool {
        // satisfied by an installed package that is not being replaced
        self.installed.iter().any(|i| {
            dep.satisfied_by(&i.info)
                && !self.pending.contains_key(&i.info.name)
                && !self.plan.iter().any(|p| p.info.name == i.info.name)
        })
    }

    fn visit(&mut self, item: Item) -> Result<()> {
        if self.state.contains_key(&item.info.name) {
            return Ok(());
        }
        self.state.insert(item.info.name.clone(), 1);
        self.visiting.push(item.info.clone());
        for d in &item.info.depends {
            let dep = Dep::parse(d)?;
            if self.plan.iter().any(|p| dep.satisfied_by(&p.info))
                || self.visiting.iter().any(|v| dep.satisfied_by(v))
            {
                continue;
            }
            if let Some(p) = self.pending.get(&dep.name).cloned() {
                if dep.satisfied_by(&p.info) {
                    self.visit(p)?;
                    continue;
                }
            }
            if self.installed_ok(&dep) {
                continue;
            }
            let cand = self
                .sync
                .find_satisfier(&dep)
                .ok_or_else(|| {
                    crate::err!(
                        "cannot satisfy dependency '{}' required by {}",
                        dep,
                        item.info.name
                    )
                })?
                .clone();
            self.visit(Item {
                info: cand.info.clone(),
                source: Source::Repo(cand),
                explicit: false,
            })?;
        }
        self.visiting.pop();
        self.state.insert(item.info.name.clone(), 2);
        self.plan.push(item);
        Ok(())
    }
}

pub fn resolve(targets: Vec<Item>, sync: &SyncDb, installed: &[Installed]) -> Result<Vec<Item>> {
    let order: Vec<String> = targets.iter().map(|t| t.info.name.clone()).collect();
    let mut r = Resolver {
        sync,
        installed,
        pending: targets
            .into_iter()
            .map(|t| (t.info.name.clone(), t))
            .collect(),
        state: HashMap::new(),
        visiting: vec![],
        plan: vec![],
    };
    for name in order {
        if let Some(item) = r.pending.get(&name).cloned() {
            r.visit(item)?;
        }
    }
    let plan = r.plan;
    check_final_state(&plan, installed, &HashSet::new())?;
    Ok(plan)
}

/// Every package in the resulting system must have its dependencies met and
/// no declared conflicts.
fn check_final_state(
    plan: &[Item],
    installed: &[Installed],
    removed: &HashSet<String>,
) -> Result<()> {
    let mut world: Vec<&PkgInfo> = plan.iter().map(|i| &i.info).collect();
    for i in installed {
        if !removed.contains(&i.info.name) && !plan.iter().any(|p| p.info.name == i.info.name) {
            world.push(&i.info);
        }
    }
    let mut problems = vec![];
    for p in &world {
        for d in &p.depends {
            let dep = Dep::parse(d)?;
            if !world.iter().any(|q| dep.satisfied_by(q)) {
                problems.push(format!("{} requires {}", p.name, dep));
            }
        }
        for c in &p.conflicts {
            let dep = Dep::parse(c)?;
            if let Some(q) = world
                .iter()
                .find(|q| q.name != p.name && dep.satisfied_by(q))
            {
                problems.push(format!("{} conflicts with {}", p.name, q.name));
            }
        }
    }
    problems.sort();
    problems.dedup();
    if !problems.is_empty() {
        bail!(
            "transaction would break the system:\n  {}",
            problems.join("\n  ")
        );
    }
    Ok(())
}

// ------------------------------------------------------------ installing

fn require_root(cfg: &Config) -> Result<()> {
    if cfg.is_live_root() && util::euid() != Some(0) {
        bail!("you must be root to change the system (use sudo)");
    }
    Ok(())
}

/// `yone install` (targets may be package names or local .yon files).
pub fn install(cfg: &Config, ui: &Ui, targets: &[String], opts: &Opts) -> Result<()> {
    require_root(cfg)?;
    let _lock = Lock::acquire(cfg)?;
    let sync = SyncDb::load(cfg, ui)?;
    let db = LocalDb::open(&cfg.dbpath);
    let installed = db.list()?;
    let mut items = vec![];
    let mut mark_explicit = vec![];
    for t in targets {
        let path = Path::new(t);
        if t.ends_with(".yon") && path.is_file() {
            if cfg.siglevel != SigLevel::Never && !opts.allow_unsigned {
                let sig = util::suffixed(path, ".sig");
                let key = sign::verify(path, &sig, &cfg.keydir)?;
                ui.info(&format!("{t}: signature ok ({key})"));
            } else {
                ui.warn(&format!("{t}: installing WITHOUT signature verification"));
            }
            let info = archive::read_info(path)?;
            items.push(Item {
                info,
                source: Source::File(path.canonicalize()?),
                explicit: !opts.asdeps,
            });
            continue;
        }
        let e = sync
            .find(t)
            .ok_or_else(|| crate::err!("target not found: {t}"))?;
        let mut explicit = !opts.asdeps;
        if let Some(i) = installed.iter().find(|i| i.info.name == e.info.name) {
            if i.info.version == e.info.version {
                // a reinstall repairs files; it does not change why the package is here
                explicit = i.explicit && !opts.asdeps;
            }
            if i.info.version == e.info.version && !opts.reinstall {
                ui.info(&format!(
                    "{} {} is up to date -- skipping",
                    t, i.info.version
                ));
                if !i.explicit && !opts.asdeps {
                    mark_explicit.push(t.clone());
                }
                continue;
            }
        }
        items.push(Item {
            info: e.info.clone(),
            source: Source::Repo(e.clone()),
            explicit,
        });
    }
    for n in mark_explicit {
        db.set_reason(&n, true)?;
    }
    if items.is_empty() {
        ui.success("nothing to do");
        return Ok(());
    }
    let plan = resolve(items, &sync, &installed)?;
    run_plan(cfg, ui, &sync, &db, &installed, plan, opts)
}

/// `yone upgrade`: install every newer version available in the indexes.
pub fn upgrade(cfg: &Config, ui: &Ui, opts: &Opts) -> Result<()> {
    require_root(cfg)?;
    let _lock = Lock::acquire(cfg)?;
    let sync = SyncDb::load(cfg, ui)?;
    let db = LocalDb::open(&cfg.dbpath);
    let installed = db.list()?;
    let mut items = vec![];
    for i in &installed {
        if let Some(e) = sync.find(&i.info.name) {
            match vercmp(&e.info.version, &i.info.version) {
                std::cmp::Ordering::Greater if cfg.hold.contains(&i.info.name) => {
                    ui.info(&format!(
                        "{}: held ({} -> {} available; see `hold` in yone.conf)",
                        i.info.name, i.info.version, e.info.version
                    ))
                }
                std::cmp::Ordering::Greater => items.push(Item {
                    info: e.info.clone(),
                    source: Source::Repo(e.clone()),
                    explicit: false,
                }),
                std::cmp::Ordering::Less => ui.warn(&format!(
                    "{}: local {} is newer than {} {}",
                    i.info.name, i.info.version, e.repo, e.info.version
                )),
                _ => {}
            }
        }
    }
    if items.is_empty() {
        ui.success("system is up to date");
        return Ok(());
    }
    let plan = resolve(items, &sync, &installed)?;
    run_plan(cfg, ui, &sync, &db, &installed, plan, opts)
}

fn print_plan(ui: &Ui, plan: &[Item], installed: &[Installed]) {
    ui.header(&format!("Packages ({})", plan.len()));
    let mut isize = 0u64;
    let mut dl = 0u64;
    for it in plan {
        let old = installed.iter().find(|i| i.info.name == it.info.name);
        let change = match old {
            Some(o) if o.info.version != it.info.version => {
                format!("{} → {}", o.info.version, ui.accent(&it.info.version))
            }
            Some(_) => format!("{} {}", it.info.version, ui.dim("(reinstall)")),
            None => ui.accent(&it.info.version),
        };
        let why = if it.explicit {
            String::new()
        } else {
            ui.dim("  [dependency]")
        };
        ui.info(&format!("{:<28} {}{}", it.info.name, change, why));
        isize += it.info.isize;
        if let Source::Repo(e) = &it.source {
            dl += e.csize;
        }
    }
    ui.info(&ui.dim(&format!(
        "download {}   installed size {}",
        human_bytes(dl),
        human_bytes(isize)
    )));
}

fn run_plan(
    cfg: &Config,
    ui: &Ui,
    sync: &SyncDb,
    db: &LocalDb,
    installed: &[Installed],
    plan: Vec<Item>,
    opts: &Opts,
) -> Result<()> {
    print_plan(ui, &plan, installed);
    if !ui.confirm("Proceed with installation?")? {
        bail!("aborted by user");
    }
    catch_interrupts();

    // 1. fetch into the cache
    fs::create_dir_all(&cfg.cachedir)?;
    let mut jobs = vec![];
    let mut files: Vec<PathBuf> = vec![];
    for it in &plan {
        match &it.source {
            Source::File(p) => files.push(p.clone()),
            Source::Repo(e) => {
                let dest = cfg.cachedir.join(&e.filename);
                let cached = dest.exists()
                    && sha256::file_hex(&dest)
                        .map(|h| h == e.sha256)
                        .unwrap_or(false);
                if !cached {
                    if opts.offline {
                        bail!("offline mode: {} is not in the cache", e.filename);
                    }
                    let _ = fs::remove_file(&dest);
                    jobs.push(Job {
                        label: e.filename.clone(),
                        urls: sync.urls(cfg, e),
                        dest: dest.clone(),
                        size: Some(e.csize),
                    });
                }
                files.push(dest);
            }
        }
    }
    if !jobs.is_empty() {
        ui.header(&format!("Downloading {} package(s)", jobs.len()));
        fetch_all(jobs, cfg.parallel, ui)?;
    }

    // 2. integrity
    ui.header("Checking integrity");
    for (it, f) in plan.iter().zip(&files) {
        if let Source::Repo(e) = &it.source {
            let h = sha256::file_hex(f)?;
            if h != e.sha256 {
                let _ = fs::remove_file(f);
                bail!(
                    "{}: checksum mismatch (expected {}, got {}); the cached file was removed",
                    e.filename,
                    e.sha256,
                    h
                );
            }
        }
    }

    // 3. listings + metadata binding + conflicts
    // resolutions are cached from here to the end of the extraction
    let resolve_cache = util::ResolveCache::enable();
    ui.header("Checking file conflicts");
    let owners = db.owners()?;
    let mut claimed: HashMap<String, String> = HashMap::new();
    let mut listings = vec![];
    let mut conflicts = vec![];
    for (it, f) in plan.iter().zip(&files) {
        let l = archive::list(f)?;
        if l.info.arch != cfg.arch && l.info.arch != "any" {
            bail!(
                "{} is built for {}, this system is {}",
                l.info.name,
                l.info.arch,
                cfg.arch
            );
        }
        if l.info.name != it.info.name || l.info.version != it.info.version {
            bail!(
                "{}: package metadata ({} {}) does not match the index ({} {})",
                f.display(),
                l.info.name,
                l.info.version,
                it.info.name,
                it.info.version
            );
        }
        let mut l = l;
        for e in l.entries.iter_mut() {
            // `lib/x` and `usr/lib/x` are one file when lib -> usr/lib
            e.path = resolve_entry(&cfg.root, &e.path, &e.kind)?;
        }
        for e in &l.entries {
            let target = cfg.root.join(&e.path);
            if e.kind == Kind::Dir {
                if let Ok(m) = fs::symlink_metadata(&target) {
                    if !m.is_dir() && !fs::metadata(&target).map(|m| m.is_dir()).unwrap_or(false) {
                        conflicts.push(format!(
                            "{}: directory in {} but a file on disk",
                            e.path, it.info.name
                        ));
                    }
                }
                continue;
            }
            if let Some(other) = claimed.insert(e.path.clone(), it.info.name.clone()) {
                conflicts.push(format!(
                    "{}: present in both {} and {}",
                    e.path, other, it.info.name
                ));
                continue;
            }
            if let Ok(m) = fs::symlink_metadata(&target) {
                if m.is_dir() {
                    conflicts.push(format!(
                        "{}: is a directory on disk ({})",
                        e.path, it.info.name
                    ));
                    continue;
                }
                match owners.get(&e.path) {
                    Some(o) if o == &it.info.name => {}
                    Some(o) if plan.iter().any(|p| &p.info.name == o) => {} // moving between upgraded packages
                    Some(o) => {
                        conflicts.push(format!("{}: exists in filesystem (owned by {})", e.path, o))
                    }
                    None if opts.overwrite => {}
                    None => conflicts.push(format!(
                        "{}: exists in filesystem ({})",
                        e.path, it.info.name
                    )),
                }
            }
        }
        listings.push(l);
    }
    if !conflicts.is_empty() {
        let more = if conflicts.len() > 20 {
            format!("\n  ... and {} more", conflicts.len() - 20)
        } else {
            String::new()
        };
        conflicts.truncate(20);
        bail!(
            "file conflicts:\n  {}{}\n(use --overwrite to replace unowned files)",
            conflicts.join("\n  "),
            more
        );
    }

    // 4. extraction under the journal
    ui.header("Installing");
    let mut journal = Journal::create(cfg)?;
    let root_canon = fs::canonicalize(&cfg.root).ctx(|| format!("root {}", cfg.root.display()))?;
    let total = plan.len();
    let total_bytes: u64 = plan.iter().map(|it| it.info.isize).sum();
    let result: Result<Vec<(Installed, Vec<FileRec>)>> = (|| {
        let dir_ok = std::sync::Mutex::new(HashSet::new());
        let fault_budget = std::sync::Mutex::new(fault_injection());
        let owners_m = std::sync::Mutex::new(Owners::new(&cfg.root));
        let bytes = std::sync::atomic::AtomicU64::new(0);
        let mut olds = vec![];
        for it in &plan {
            let old = db.get(&it.info.name)?;
            let old_files: HashMap<String, FileRec> = if old.is_some() {
                db.files(&it.info.name)?
                    .into_iter()
                    .map(|r| (r.path.clone(), r))
                    .collect()
            } else {
                HashMap::new()
            };
            olds.push((old, old_files));
        }
        let (first, rest) = extraction_order(&listings);
        let mut recs_of: Vec<Option<Vec<FileRec>>> = vec![None; plan.len()];
        let mut finished = 0usize;
        let done_line = |n: usize, finished: usize| {
            let it = &plan[n];
            if ui.tty {
                ui.progress_end();
                eprintln!(
                    "  {} {} {}",
                    ui.green("✓"),
                    it.info.name,
                    ui.dim(&it.info.version)
                );
            } else {
                ui.stage(finished, total, &format!("{} {}", it.info.name, it.info.version));
            }
        };
        let run = |n: usize, prog: Option<&Progress>| {
            extract(
                cfg,
                ui,
                &journal,
                &root_canon,
                &dir_ok,
                &files[n],
                &plan[n].info,
                &listings[n].entries,
                &olds[n].1,
                &fault_budget,
                &owners_m,
                prog,
                &bytes,
            )
        };
        // What other packages build on (symlinked directories, accounts)
        // goes first, one package at a time, in the order of the plan.
        for &n in &first {
            let base = bytes.load(std::sync::atomic::Ordering::Relaxed);
            let prog = Progress {
                n: finished + 1,
                total,
                base,
                total_bytes,
            };
            recs_of[n] = Some(run(n, Some(&prog))?);
            finished += 1;
            done_line(n, finished);
        }
        // The rest touches disjoint paths (the conflict check made sure):
        // extracted side by side, a package per thread.
        let threads = std::env::var("YONE_EXTRACT_THREADS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or_else(|| {
                std::thread::available_parallelism()
                    .map(|n| n.get())
                    .unwrap_or(1)
                    .clamp(1, 8)
            })
            .clamp(1, 64)
            .min(rest.len().max(1));
        if threads <= 1 {
            for &n in &rest {
                let base = bytes.load(std::sync::atomic::Ordering::Relaxed);
                let prog = Progress {
                    n: finished + 1,
                    total,
                    base,
                    total_bytes,
                };
                recs_of[n] = Some(run(n, Some(&prog))?);
                finished += 1;
                done_line(n, finished);
            }
        } else {
            let next = std::sync::atomic::AtomicUsize::new(0);
            let stop = std::sync::atomic::AtomicBool::new(false);
            let mut failure = None;
            std::thread::scope(|sc| {
                let (tx, rx) = std::sync::mpsc::channel();
                for _ in 0..threads {
                    let tx = tx.clone();
                    let (next, stop, rest, run) = (&next, &stop, &rest, &run);
                    sc.spawn(move || {
                        let _cache = util::ResolveCache::enable();
                        while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                            let k = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                            let Some(&n) = rest.get(k) else { break };
                            let r = run(n, None);
                            if r.is_err() {
                                stop.store(true, std::sync::atomic::Ordering::Relaxed);
                            }
                            if tx.send((n, r)).is_err() {
                                break;
                            }
                        }
                    });
                }
                drop(tx);
                loop {
                    match rx.recv_timeout(std::time::Duration::from_millis(80)) {
                        Ok((n, Ok(recs))) => {
                            recs_of[n] = Some(recs);
                            finished += 1;
                            done_line(n, finished);
                        }
                        Ok((_, Err(e))) => {
                            failure.get_or_insert(e);
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                    }
                    if ui.tty && failure.is_none() {
                        let b = bytes.load(std::sync::atomic::Ordering::Relaxed);
                        let frac = if total_bytes > 0 {
                            b as f64 / total_bytes as f64
                        } else {
                            finished as f64 / total.max(1) as f64
                        };
                        ui.status(
                            "installing",
                            frac.min(1.0),
                            &format!("{finished}/{total}"),
                            &format!("{threads} at a time"),
                        );
                    }
                }
            });
            if let Some(e) = failure {
                return Err(e);
            }
        }
        let mut out = vec![];
        for (n, it) in plan.iter().enumerate() {
            let recs = recs_of[n].take().expect("every package extracted");
            let old = &olds[n].0;
            // reasons only ever widen (dependency -> explicit), except that
            // an explicit reinstall keeps the existing reason (see install())
            let explicit = match old {
                Some(o) if o.info.version == it.info.version => it.explicit,
                Some(o) => o.explicit || it.explicit,
                None => it.explicit,
            };
            out.push((
                Installed {
                    info: it.info.clone(),
                    explicit,
                    installdate: util::now(),
                },
                recs,
            ));
        }
        Ok(out)
    })();
    drop(resolve_cache);
    let done = match result {
        Ok(d) => d,
        Err(e) => {
            ui.progress_end();
            ui.warn("installation failed — rolling back");
            drop(journal);
            let n = rollback(cfg)?;
            util::log(cfg, &format!("transaction rolled back: {e}"));
            ui.info(&format!(
                "rolled back {n} change(s); the system is unchanged"
            ));
            return Err(e);
        }
    };
    // 5. stage the database, journal the leftovers of upgrades, commit
    let new_paths: HashSet<&str> = done
        .iter()
        .flat_map(|(_, r)| r.iter().map(|f| f.path.as_str()))
        .collect();
    let mut stale: Vec<(String, char)> = vec![];
    for (inst, _) in &done {
        if installed.iter().any(|i| i.info.name == inst.info.name) {
            for f in db.files(&inst.info.name)? {
                if new_paths.contains(f.path.as_str()) {
                    continue;
                }
                let mine = owners
                    .get(&f.path)
                    .map(|o| o == &inst.info.name)
                    .unwrap_or(true);
                if f.kind == 'd' || mine {
                    stale.push((f.path, if f.kind == 'd' { 'R' } else { 'S' }));
                }
            }
        }
    }
    stale.sort_by(|a, b| b.cmp(a)); // children before their directories
    stale.dedup();
    let result: Result<()> = (|| {
        for (inst, recs) in &done {
            db.stage(inst, recs)?;
        }
        for (p, tag) in &stale {
            journal.rec(*tag, &cfg.root.join(p))?;
        }
        check_interrupted()?;
        crash_point("staged");
        journal.commit_point(&cfg.root)
    })();
    if let Err(e) = result {
        ui.warn("installation failed — rolling back");
        drop(journal);
        rollback(cfg)?;
        return Err(e);
    }
    drop(journal);
    roll_forward(cfg)?;
    let mut ops = vec![];
    for (inst, _) in &done {
        let (op, line) = match installed.iter().find(|i| i.info.name == inst.info.name) {
            Some(o) if o.info.version == inst.info.version => (
                hooks::Op::Upgrade,
                format!("reinstalled {} ({})", inst.info.name, inst.info.version),
            ),
            Some(o) => (
                hooks::Op::Upgrade,
                format!(
                    "upgraded {} ({} -> {})",
                    inst.info.name, o.info.version, inst.info.version
                ),
            ),
            None => (
                hooks::Op::Install,
                format!("installed {} ({})", inst.info.name, inst.info.version),
            ),
        };
        util::log(cfg, &line);
        if !ops.contains(&op) {
            ops.push(op);
        }
    }
    let mut changed: Vec<String> = done
        .iter()
        .flat_map(|(_, r)| r.iter().map(|f| f.path.clone()))
        .collect();
    changed.extend(stale.into_iter().map(|(p, _)| p));
    hooks::run(cfg, ui, &ops, &changed);
    ui.success(&format!("{} package(s) installed", done.len()));
    Ok(())
}

#[cfg(debug_assertions)]
fn fault_injection() -> Option<usize> {
    std::env::var("YONE_TEST_FAIL_AFTER")
        .ok()
        .and_then(|v| v.parse().ok())
}
#[cfg(not(debug_assertions))]
fn fault_injection() -> Option<usize> {
    None
}

/// Which packages to extract alone, first (in plan order), and which can be
/// extracted in parallel. Alone: a package with a symlink that is a directory
/// of another package's paths (`lib -> usr/lib`: the others' paths resolve
/// through it), or one shipping the account files other packages' owners are
/// looked up in.
fn extraction_order(listings: &[archive::Listing]) -> (Vec<usize>, Vec<usize>) {
    let mut dirs: HashMap<&str, usize> = HashMap::new();
    const SHARED: usize = usize::MAX;
    for (n, l) in listings.iter().enumerate() {
        for e in &l.entries {
            let mut p = e.path.as_str();
            while let Some((d, _)) = p.rsplit_once('/') {
                let slot = dirs.entry(d).or_insert(n);
                if *slot != n {
                    *slot = SHARED;
                }
                p = d;
            }
        }
    }
    let (mut first, mut rest) = (vec![], vec![]);
    for (n, l) in listings.iter().enumerate() {
        let alone = l.entries.iter().any(|e| {
            matches!(e.path.as_str(), "etc/passwd" | "etc/group")
                || (matches!(e.kind, Kind::Symlink(_))
                    && dirs.get(e.path.as_str()).is_some_and(|&o| o != n))
        });
        if alone {
            first.push(n);
        } else {
            rest.push(n);
        }
    }
    (first, rest)
}

fn lock<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn ensure_dirs(
    dir: &Path,
    root_canon: &Path,
    j: &Journal,
    ok: &std::sync::Mutex<HashSet<PathBuf>>,
) -> Result<()> {
    if ok.lock().unwrap_or_else(|e| e.into_inner()).contains(dir) {
        return Ok(());
    }
    let mut missing = vec![];
    let mut d = dir;
    while fs::symlink_metadata(d).is_err() {
        missing.push(d.to_path_buf());
        d = match d.parent() {
            Some(p) => p,
            None => break,
        };
    }
    for m in missing.iter().rev() {
        j.rec('D', m)?;
        create_dir_shared(m)?;
    }
    let canon = fs::canonicalize(dir)?;
    if !canon.starts_with(root_canon) {
        bail!(
            "{} resolves outside the installation root ({})",
            dir.display(),
            canon.display()
        );
    }
    ok.lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(dir.to_path_buf());
    Ok(())
}

/// mkdir, where another package of the transaction may be creating the same
/// directory at the same time. Whether this call made it.
fn create_dir_shared(d: &Path) -> Result<bool> {
    match fs::create_dir(d) {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && d.is_dir() => Ok(false),
        Err(e) => Err(e).ctx(|| format!("mkdir {}", d.display())),
    }
}

/// Gives installed files their owner. Names are looked up in the target
/// root's own /etc/passwd and /etc/group (read again after each package: the
/// one that creates a group may be in the same transaction); the ids
/// recorded in the package are the fallback.
pub struct Owners {
    root: PathBuf,
    can_chown: bool,
    warned: HashSet<String>,
}

impl Owners {
    pub fn new(root: &Path) -> Owners {
        Owners {
            root: root.to_path_buf(),
            can_chown: util::euid() == Some(0),
            warned: HashSet::new(),
        }
    }

    fn warn_once(&mut self, ui: &Ui, key: String, msg: String) {
        if self.warned.insert(key) {
            ui.progress_end();
            ui.warn(&msg);
        }
    }

    /// The ids `owner` stands for on the target system.
    fn ids(&mut self, ui: &Ui, owner: &crate::tar::Owner) -> (u32, u32) {
        let uid = util::account_id(&self.root, "etc/passwd", &owner.user).unwrap_or_else(|| {
            if !owner.user.is_empty() {
                self.warn_once(
                    ui,
                    format!("u:{}", owner.user),
                    format!(
                        "user {} is unknown on the target system: id {} is used",
                        owner.user, owner.uid
                    ),
                );
            }
            owner.uid
        });
        let gid = util::account_id(&self.root, "etc/group", &owner.group).unwrap_or_else(|| {
            if !owner.group.is_empty() {
                self.warn_once(
                    ui,
                    format!("g:{}", owner.group),
                    format!(
                        "group {} is unknown on the target system: id {} is used",
                        owner.group, owner.gid
                    ),
                );
            }
            owner.gid
        });
        (uid, gid)
    }

    /// Apply `owner` to `p` (before its mode: chown clears setuid/setgid).
    /// Returns what the database records.
    fn apply(
        &mut self,
        ui: &Ui,
        p: &Path,
        owner: &crate::tar::Owner,
        rel: &str,
    ) -> Result<Option<String>> {
        if owner.is_root() {
            return Ok(None);
        }
        let spec = owner.spec();
        if !self.can_chown {
            self.warn_once(
                ui,
                format!("r:{rel}"),
                format!("/{rel} should belong to {spec}: only root can do that"),
            );
            return Ok(Some(spec));
        }
        let (uid, gid) = self.ids(ui, owner);
        util::set_owner(p, uid, gid).ctx(|| format!("chown {}", p.display()))?;
        Ok(Some(spec))
    }
}

fn set_mode(p: &Path, mode: u32) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(p, fs::Permissions::from_mode(mode & 0o7777))
        .ctx(|| format!("chmod {}", p.display()))
}

fn resolve_entry(root: &Path, path: &str, kind: &Kind) -> Result<String> {
    if *kind == Kind::Dir {
        util::resolve_dir_in_root(root, path)
    } else {
        util::resolve_in_root(root, path)
    }
}

/// Move the new object (already written at `tmp`) into place, keeping the
/// previous one aside for rollback.
fn place(j: &Journal, tmp: &Path, target: &Path) -> Result<()> {
    let old = fs::symlink_metadata(target);
    let link_involved = old
        .as_ref()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
        || fs::symlink_metadata(tmp)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false);
    if link_involved {
        util::forget_resolutions();
    }
    if old.is_ok() {
        j.rec('B', target)?;
        fs::rename(target, util::suffixed(target, ".yone-old"))
            .ctx(|| format!("move aside {}", target.display()))?;
    }
    j.rec('N', target)?;
    fs::rename(tmp, target).ctx(|| format!("install {}", target.display()))?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
/// Where one package stands in its transaction, for the progress bar.
struct Progress {
    n: usize,
    total: usize,
    base: u64,
    total_bytes: u64,
}

#[allow(clippy::too_many_arguments)]
fn extract(
    cfg: &Config,
    ui: &Ui,
    j: &Journal,
    root_canon: &Path,
    dir_ok: &std::sync::Mutex<HashSet<PathBuf>>,
    file: &Path,
    info: &PkgInfo,
    planned: &[crate::tar::Entry],
    old_files: &HashMap<String, FileRec>,
    fault: &std::sync::Mutex<Option<usize>>,
    owners: &std::sync::Mutex<Owners>,
    prog: Option<&Progress>,
    bytes: &std::sync::atomic::AtomicU64,
) -> Result<Vec<FileRec>> {
    let mut tr = archive::open_tar(file)?;
    let mut planned = planned.iter();
    let mut recs = vec![];
    let mut done_bytes = 0u64;
    let mut throttle = Throttle::new();
    while let Some(e) = tr.next_entry().ctx(|| file.display().to_string())? {
        if e.path == archive::INFO_NAME || e.path == archive::FILES_NAME {
            continue;
        }
        check_interrupted()?;
        #[cfg(debug_assertions)]
        if let Some(n) = std::env::var("YONE_TEST_SIGINT_AFTER")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
        {
            if recs.len() == n {
                // SAFETY: raise only delivers a signal to this process.
                unsafe { raise(2) };
            }
        }
        let spelled = util::safe_rel_path(&e.path)?;
        let rel = resolve_entry(&cfg.root, &spelled, &e.kind)?;
        // The conflict check ran on the paths resolved before extraction. A
        // symlinked directory created meanwhile (by this or an earlier package
        // of the transaction) must not redirect a file somewhere unchecked.
        let plan = planned.next();
        // the conflict check may have read the package's file list instead of
        // its content: the content must be exactly what the list says
        if let Some(p) = plan {
            let same_kind = match (&p.kind, &e.kind) {
                (Kind::Hardlink(a), Kind::Hardlink(b)) => util::safe_rel_path(b)? == *a,
                (a, b) => a == b,
            };
            if !same_kind
                || p.mode != e.mode
                || p.size != e.size
                || p.owner != e.owner
                || p.xattrs != e.xattrs
            {
                bail!(
                    "{}: /{} differs from the package's file list; refusing a damaged package",
                    info.name,
                    spelled
                );
            }
        }
        if plan.map(|p| p.path.as_str()) != Some(rel.as_str()) {
            bail!(
                "{}: /{} would be redirected to /{} by a symlink installed in the same \
                 transaction; refusing",
                info.name,
                spelled,
                rel
            );
        }
        if let Some(n) = fault.lock().unwrap_or_else(|e| e.into_inner()).as_mut() {
            if *n == 0 {
                bail!("injected failure (YONE_TEST_FAIL_AFTER)");
            }
            *n -= 1;
        }
        let target = cfg.root.join(&rel);
        if let Some(parent) = target.parent() {
            ensure_dirs(parent, root_canon, j, dir_ok)?;
        }
        match &e.kind {
            Kind::Dir => {
                // an existing directory keeps its owner and mode
                let created = fs::metadata(&target).is_err() && {
                    j.rec('D', &target)?;
                    create_dir_shared(&target)?
                };
                let owner = if created {
                    let o = lock(owners).apply(ui, &target, &e.owner, &rel)?;
                    set_mode(&target, e.mode)?;
                    o
                } else {
                    (!e.owner.is_root()).then(|| e.owner.spec())
                };
                recs.push(FileRec {
                    kind: 'd',
                    mode: e.mode,
                    data: "-".into(),
                    path: rel,
                    owner,
                });
            }
            Kind::File => {
                let tmp = util::suffixed(&target, ".yone-new");
                j.rec('T', &target)?;
                let sha = {
                    // never write through something already sitting at the
                    // temp path: create it new, removing a leftover only when
                    // there is one (one system call per file, not two)
                    let create = || OpenOptions::new().write(true).create_new(true).open(&tmp);
                    let out = match create() {
                        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                            fs::remove_file(&tmp)?;
                            create()
                        }
                        r => r,
                    }
                    .ctx(|| format!("create {}", tmp.display()))?;
                    let mut w = HashingWriter::new(BufWriter::with_capacity(1 << 18, out));
                    tr.copy_data(&mut w)?;
                    w.flush()?;
                    w.hasher.finalize_hex()
                };
                let owner = lock(owners).apply(ui, &tmp, &e.owner, &rel)?;
                set_mode(&tmp, e.mode)?;
                for (k, v) in &e.xattrs {
                    if k != util::CAPABILITY_XATTR {
                        continue; // only file capabilities are honoured
                    }
                    if let Err(err) = util::set_capability(&tmp, v) {
                        ui.progress_end();
                        ui.warn(&format!(
                            "/{rel}: cannot set its file capability ({err}); it may lack privileges it needs"
                        ));
                    }
                }
                done_bytes += e.size;
                bytes.fetch_add(e.size, std::sync::atomic::Ordering::Relaxed);
                // configuration files changed by the administrator are preserved
                let user_modified = (info.backup.contains(&rel) || info.backup.contains(&spelled))
                    && old_files
                        .get(&rel)
                        .map(|o| {
                            sha256::file_hex(&target)
                                .map(|d| d != o.data)
                                .unwrap_or(false)
                        })
                        .unwrap_or(false);
                if user_modified {
                    let side = util::suffixed(&target, ".yonenew");
                    j.rec('N', &side)?;
                    fs::rename(&tmp, &side)?;
                    ui.progress_end();
                    ui.warn(&format!(
                        "/{rel} was modified locally; new version saved as /{rel}.yonenew"
                    ));
                    // Record what the *package* shipped, not the admin's file:
                    // the next upgrade must still see the file as modified.
                    recs.push(FileRec {
                        kind: 'f',
                        mode: e.mode,
                        data: sha,
                        path: rel,
                        owner,
                    });
                    continue;
                }
                place(j, &tmp, &target)?;
                recs.push(FileRec {
                    kind: 'f',
                    mode: e.mode,
                    data: sha,
                    path: rel,
                    owner,
                });
            }
            Kind::Symlink(t) => {
                let tmp = util::suffixed(&target, ".yone-new");
                j.rec('T', &target)?;
                let _ = fs::remove_file(&tmp);
                std::os::unix::fs::symlink(t, &tmp)
                    .ctx(|| format!("symlink {}", target.display()))?;
                let owner = lock(owners).apply(ui, &tmp, &e.owner, &rel)?;
                place(j, &tmp, &target)?;
                recs.push(FileRec {
                    kind: 'l',
                    mode: 0o777,
                    data: t.clone(),
                    path: rel,
                    owner,
                });
            }
            Kind::Hardlink(t) => {
                let src = cfg.root.join(util::resolve_in_root(&cfg.root, t)?);
                if let Some(parent) = src.parent() {
                    let canon = fs::canonicalize(parent).ctx(|| format!("link source {t}"))?;
                    if !canon.starts_with(root_canon) {
                        bail!("hard link {rel} -> {t} resolves outside the installation root");
                    }
                }
                let tmp = util::suffixed(&target, ".yone-new");
                j.rec('T', &target)?;
                let _ = fs::remove_file(&tmp);
                fs::hard_link(&src, &tmp).ctx(|| format!("link {}", target.display()))?;
                place(j, &tmp, &target)?;
                let sha = sha256::file_hex(&target)?;
                recs.push(FileRec {
                    kind: 'f',
                    mode: e.mode,
                    data: sha,
                    path: rel,
                    owner: None,
                });
            }
        }
        if let Some(prog) = prog.filter(|_| ui.tty && throttle.ready()) {
            // the whole transaction, by installed size
            let frac = if prog.total_bytes > 0 {
                (prog.base + done_bytes.min(info.isize)) as f64 / prog.total_bytes as f64
            } else {
                (prog.n - 1) as f64 / prog.total.max(1) as f64
            };
            ui.status(
                "installing",
                frac,
                &format!("{}/{}", prog.n, prog.total),
                &format!("{} › {}", info.name, e.path),
            );
        }
    }
    tr.into_inner()
        .finish()
        .ctx(|| file.display().to_string())?;
    ui.progress_end();
    Ok(recs)
}

// --------------------------------------------------------------- removal

pub fn remove(cfg: &Config, ui: &Ui, names: &[String], opts: &Opts) -> Result<()> {
    require_root(cfg)?;
    let _lock = Lock::acquire(cfg)?;
    let db = LocalDb::open(&cfg.dbpath);
    let installed = db.list()?;
    let mut set: HashSet<String> = HashSet::new();
    for n in names {
        if !installed.iter().any(|i| &i.info.name == n) {
            bail!("target not installed: {n}");
        }
        set.insert(n.clone());
    }
    if opts.cascade {
        loop {
            let before = set.len();
            for i in &installed {
                if set.contains(&i.info.name) {
                    continue;
                }
                let needs_removed = i
                    .info
                    .depends
                    .iter()
                    .filter_map(|d| Dep::parse(d).ok())
                    .any(|dep| {
                        !installed
                            .iter()
                            .any(|o| !set.contains(&o.info.name) && dep.satisfied_by(&o.info))
                    });
                if needs_removed {
                    set.insert(i.info.name.clone());
                }
            }
            if set.len() == before {
                break;
            }
        }
    }
    check_final_state(&[], &installed, &set).map_err(|e| {
        crate::err!(
            "{}\n(use --cascade to also remove the packages that depend on them)",
            e
        )
    })?;
    remove_set(cfg, ui, &db, &installed, &set)
}

/// `yone autoremove`: packages installed as dependencies that nothing needs anymore.
pub fn autoremove(cfg: &Config, ui: &Ui) -> Result<()> {
    require_root(cfg)?;
    let _lock = Lock::acquire(cfg)?;
    let db = LocalDb::open(&cfg.dbpath);
    let installed = db.list()?;
    let set = orphans(&installed);
    if set.is_empty() {
        ui.success("no orphaned packages");
        return Ok(());
    }
    remove_set(cfg, ui, &db, &installed, &set)
}

pub fn orphans(installed: &[Installed]) -> HashSet<String> {
    let mut set: HashSet<String> = HashSet::new();
    loop {
        let before = set.len();
        for i in installed {
            if i.explicit || set.contains(&i.info.name) {
                continue;
            }
            let needed = installed
                .iter()
                .filter(|o| !set.contains(&o.info.name) && o.info.name != i.info.name)
                .any(|o| {
                    o.info
                        .depends
                        .iter()
                        .filter_map(|d| Dep::parse(d).ok())
                        .any(|dep| dep.satisfied_by(&i.info))
                });
            if !needed {
                set.insert(i.info.name.clone());
            }
        }
        if set.len() == before {
            return set;
        }
    }
}

fn remove_set(
    cfg: &Config,
    ui: &Ui,
    db: &LocalDb,
    installed: &[Installed],
    set: &HashSet<String>,
) -> Result<()> {
    let mut names: Vec<&String> = set.iter().collect();
    names.sort();
    ui.header(&format!("Removing ({})", names.len()));
    let mut freed = 0;
    for n in &names {
        let i = installed.iter().find(|i| &&i.info.name == n).unwrap();
        freed += i.info.isize;
        ui.info(&format!("{:<28} {}", n, i.info.version));
    }
    ui.info(&ui.dim(&format!("frees {}", human_bytes(freed))));
    if !ui.confirm("Proceed with removal?")? {
        bail!("aborted by user");
    }
    catch_interrupts();
    let owners = db.owners()?;
    let mut journal = Journal::create(cfg)?;
    let mut dirs: Vec<String> = vec![];
    let mut changed: Vec<String> = vec![];
    let result: Result<()> = (|| {
        for n in &names {
            let info = &installed.iter().find(|i| &&i.info.name == n).unwrap().info;
            for f in db.files(n)? {
                if f.kind == 'd' {
                    dirs.push(f.path.clone());
                    continue;
                }
                if owners.get(&f.path).map(|o| o != *n).unwrap_or(false) {
                    continue;
                }
                let p = cfg.root.join(&f.path);
                if fs::symlink_metadata(&p).is_err() {
                    continue;
                }
                if f.kind == 'f'
                    && info.backup.contains(&f.path)
                    && sha256::file_hex(&p).map(|h| h != f.data).unwrap_or(false)
                {
                    let save = util::suffixed(&p, ".yonesave");
                    fs::copy(&p, &save)?;
                    ui.warn(&format!(
                        "/{} was modified; saved as /{}.yonesave",
                        f.path, f.path
                    ));
                }
                check_interrupted()?;
                journal.rec('B', &p)?;
                fs::rename(&p, util::suffixed(&p, ".yone-old"))
                    .ctx(|| format!("remove {}", p.display()))?;
                changed.push(f.path.clone());
            }
        }
        Ok(())
    })();
    let result = result.and_then(|_| {
        // directories that no remaining package references go away after commit
        let mut still: HashSet<String> = HashSet::new();
        for i in installed.iter().filter(|i| !set.contains(&i.info.name)) {
            still.extend(
                db.files(&i.info.name)?
                    .into_iter()
                    .filter(|f| f.kind == 'd')
                    .map(|f| f.path),
            );
        }
        dirs.sort_by(|a, b| b.cmp(a)); // children before their parents
        dirs.dedup();
        for d in dirs.iter().filter(|d| !still.contains(*d)) {
            journal.rec('R', &cfg.root.join(d))?;
        }
        for n in &names {
            db.stage_remove(n)?;
        }
        check_interrupted()?;
        crash_point("staged");
        journal.commit_point(&cfg.root)
    });
    if let Err(e) = result {
        ui.warn("removal failed — rolling back");
        drop(journal);
        rollback(cfg)?;
        util::log(cfg, &format!("transaction rolled back: {e}"));
        return Err(e);
    }
    drop(journal);
    roll_forward(cfg)?;
    for n in &names {
        let i = installed.iter().find(|i| &&i.info.name == n).unwrap();
        util::log(cfg, &format!("removed {} ({})", n, i.info.version));
    }
    hooks::run(cfg, ui, &[hooks::Op::Remove], &changed);
    ui.success(&format!("{} package(s) removed", names.len()));
    Ok(())
}

// ----------------------------------------------------------- verification

/// `yone verify`: compare installed files against the database.
pub fn verify(cfg: &Config, ui: &Ui, names: &[String]) -> Result<bool> {
    let db = LocalDb::open(&cfg.dbpath);
    let installed = db.list()?;
    let targets: Vec<&Installed> = if names.is_empty() {
        installed.iter().collect()
    } else {
        names
            .iter()
            .map(|n| {
                installed
                    .iter()
                    .find(|i| &i.info.name == n)
                    .ok_or_else(|| crate::err!("{n} is not installed"))
            })
            .collect::<Result<_>>()?
    };
    let mut bad = 0usize;
    let mut checked = 0usize;
    for i in targets {
        for f in db.files(&i.info.name)? {
            checked += 1;
            let p = cfg.root.join(&f.path);
            let problem = match f.kind {
                'd' => (!fs::metadata(&p).map(|m| m.is_dir()).unwrap_or(false))
                    .then(|| "missing directory".to_string()),
                'l' => match fs::read_link(&p) {
                    Ok(t) if t.to_string_lossy() == f.data => None,
                    Ok(t) => Some(format!(
                        "symlink points to {} (expected {})",
                        t.display(),
                        f.data
                    )),
                    Err(_) => Some("missing symlink".into()),
                },
                _ => match fs::symlink_metadata(&p) {
                    Err(_) => Some("missing file".into()),
                    Ok(m) if !m.is_file() => Some("not a regular file".into()),
                    Ok(_) => match sha256::file_hex(&p) {
                        Ok(h) if h == f.data => None,
                        Ok(_) if i.info.backup.contains(&f.path) => {
                            ui.info(&format!(
                                "{}: /{} modified (configuration file)",
                                i.info.name, f.path
                            ));
                            None
                        }
                        Ok(_) => Some("checksum mismatch".into()),
                        Err(e) => Some(e.to_string()),
                    },
                },
            };
            let problem = problem.or_else(|| {
                let spec = f.owner.as_ref()?;
                use std::os::unix::fs::MetadataExt;
                let m = fs::symlink_metadata(&p).ok()?;
                let (user, group) = spec.split_once(':')?;
                let user_ok = util::account_id(&cfg.root, "etc/passwd", user)
                    .map(|id| id == m.uid())
                    .unwrap_or(true);
                let group_ok = util::account_id(&cfg.root, "etc/group", group)
                    .map(|id| id == m.gid())
                    .unwrap_or(true);
                let have = format!(
                    "{}:{}",
                    util::account_name(&cfg.root, "etc/passwd", m.uid())
                        .unwrap_or_else(|| m.uid().to_string()),
                    util::account_name(&cfg.root, "etc/group", m.gid())
                        .unwrap_or_else(|| m.gid().to_string())
                );
                (!(user_ok && group_ok)).then(|| format!("belongs to {have} (expected {spec})"))
            });
            if let Some(why) = problem {
                bad += 1;
                println!("{} {}: /{} — {}", ui.red("✗"), i.info.name, f.path, why);
            }
        }
    }
    if bad == 0 {
        ui.success(&format!("{checked} files verified, no problems"));
        Ok(true)
    } else {
        eprintln!(
            "{} {} problem(s) in {} files",
            ui.red("error:"),
            bad,
            checked
        );
        Ok(false)
    }
}

/// `yone clean`: drop cached packages that are not the installed version.
pub fn clean(cfg: &Config, ui: &Ui, all: bool) -> Result<()> {
    require_root(cfg)?;
    let _lock = Lock::acquire(cfg)?;
    let db = LocalDb::open(&cfg.dbpath);
    let installed = db.list()?;
    let keep: HashSet<String> = installed
        .iter()
        .map(|i| format!("{}-{}-", i.info.name, i.info.version))
        .collect();
    let mut freed = 0;
    let mut n = 0;
    if let Ok(rd) = fs::read_dir(&cfg.cachedir) {
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let keep_it =
                !all && name.ends_with(".yon") && keep.iter().any(|k| name.starts_with(k.as_str()));
            if !keep_it {
                freed += e.metadata().map(|m| m.len()).unwrap_or(0);
                if fs::remove_file(e.path()).is_ok() {
                    n += 1;
                }
            }
        }
    }
    ui.success(&format!(
        "removed {n} cached file(s), freed {}",
        human_bytes(freed)
    ));
    Ok(())
}
